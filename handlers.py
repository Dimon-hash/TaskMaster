import logging
import re
import json
import html  # ✅ безопасный HTML-вывод
from datetime import datetime, timedelta, time, date, timezone as dt_timezone
from typing import List, Optional, Dict, Tuple
from io import BytesIO
from pathlib import Path

from urllib.parse import urlencode, urlparse
import ipaddress

import aiohttp
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    InputMediaPhoto,
    InputFile,
)
from telegram.ext import ContextTypes
from telegram.error import BadRequest  # ✅ безопасное редактирование клавиатуры

from database import Database
from gpt_tasks import verify_task_with_gpt
from config import settings

from zoneinfo import ZoneInfo
APP_TZ = ZoneInfo(getattr(settings, "TIMEZONE", "Europe/Moscow"))

logger = logging.getLogger(__name__)

# ======================= КЭШ/СЕССИИ/ТАЙМЗОНЫ =======================
REST_CACHE: dict[int, int] = {}            # user_id -> rest_seconds (для URL WebApp)
WORKOUT_WINDOW_CACHE: dict[int, int] = {}  # user_id -> seconds (длительность окна тренировки)
TZ_CACHE: dict[int, ZoneInfo] = {}         # user_id -> ZoneInfo
REGISTERED_CACHE: set[int] = set()

def _set_registered(user_id: int, ok: bool) -> None:
    if ok:
        REGISTERED_CACHE.add(user_id)
    else:
        REGISTERED_CACHE.discard(user_id)

def _is_registered(user_id: int) -> bool:
    return user_id in REGISTERED_CACHE

def _get_rest_seconds_cached(user_id: int) -> int:
    return int(REST_CACHE.get(user_id, 60))

def _set_rest_seconds_cached(user_id: int, seconds: int) -> None:
    REST_CACHE[user_id] = max(1, int(seconds))

def _get_window_seconds_cached(user_id: int) -> int:
    return int(WORKOUT_WINDOW_CACHE.get(user_id, 3600))

def _set_window_seconds_cached(user_id: int, seconds: int) -> None:
    WORKOUT_WINDOW_CACHE[user_id] = max(60, int(seconds))

def _set_tz_for(user_id: int, tz_name: Optional[str]) -> None:
    try:
        tz = ZoneInfo(tz_name) if tz_name else APP_TZ
    except Exception:
        tz = APP_TZ
    TZ_CACHE[user_id] = tz
def _parse_deposit_from_text(s: str, default: int = 5000) -> int:
    m = re.search(r"\d{2,6}", (s or "").replace(" ", ""))
    if not m:
        return _clamp_deposit(default)
    return _clamp_deposit(int(m.group(0)))

def _tz_for(user_id: int) -> ZoneInfo:
    return TZ_CACHE.get(user_id, APP_TZ)

def _ws_get(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    all_ws = context.application.bot_data.setdefault("workout_session", {})
    ws = all_ws.get(user_id)
    if not ws:
        ws = {"expected": 3, "results": [], "started_at": datetime.now(_tz_for(user_id))}
        all_ws[user_id] = ws
    return ws

def _ws_reset(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    context.application.bot_data.setdefault("workout_session", {}).pop(user_id, None)

# ---------------- Утилиты ----------------
def _is_admin(user_id: int) -> bool:
    try:
        return user_id in set(getattr(settings, "ADMIN_IDS", []))
    except Exception:
        return False

def _extract_image_file_id_from_message(message) -> Optional[str]:
    if not message:
        return None
    if getattr(message, "photo", None):
        return message.photo[-1].file_id
    doc = getattr(message, "document", None)
    if doc and (doc.mime_type or "").startswith("image/"):
        return doc.file_id
    return None

# ✅ Безопасное редактирование инлайн-клавиатуры (игнор «Message is not modified»)
from typing import Optional  # если не импортировал

async def _safe_edit_reply_markup(message: Message, reply_markup: Optional[InlineKeyboardMarkup]) -> None:
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise

# ✅ ФИКС: не рекурсивно, а к telegram.CallbackQuery.answer
async def _safe_cq_answer(cq, text: Optional[str] = None, **kwargs) -> None:
    try:
        await cq.answer(text=text, **kwargs)
    except BadRequest as e:
        s = str(e)
        if ("Query is too old" in s) or ("query id is invalid" in s) or ("response timeout expired" in s):
            return
        raise

# ---------------- Клавиатуры (главное меню) ----------------
from urllib.parse import urlencode, urlparse  # (объединил импорт)

def _is_private_host(netloc: str) -> bool:
    host = (netloc or "").split(":", 1)[0].lower()
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        # доменное имя — считаем публичным
        return False
def _webapp_base() -> str:
    """
    Источник правды — settings.WEBAPP_ORIGIN (нормализуется в config.py).
    Локально — http://127.0.0.1:8000
    Ничего не форсим здесь, чтобы не ловить SSL-ошибки.
    """
    # если есть WEBAPP_ORIGIN — используем её (она уже без завершающего /)
    origin = getattr(settings, "WEBAPP_ORIGIN", None)
    if origin:
        return str(origin).rstrip("/")

    # иначе fallback на свойство WEBAPP_URL (в нём есть завершающий /)
    base = str(getattr(settings, "WEBAPP_URL", "http://127.0.0.1:8000/")).strip().rstrip("/")
    # если вдруг пришло без схемы — добавим http
    if not base.startswith(("http://", "https://")):
        base = "http://" + base

    # если хост приватный — оставляем как есть (http)
    pr = urlparse(base)
    if _is_private_host(pr.netloc):
        return base

    # публичный хост: не насилуем схему, берём ту, что уже стоит
    return base

def _build_webapp_url(params: dict) -> str:
    return _webapp_base() + "/?" + urlencode(params, safe=":/?&=,+@")

async def _build_workout_keyboard(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> ReplyKeyboardMarkup:
    # читаем rest/window из кэша
    rest_sec = _get_rest_seconds_cached(user_id)
    window_sec = _get_window_seconds_cached(user_id)

    # тащим план из training_form
    plan_text = None
    plan_video = None
    try:
        async with Database.acquire() as conn:
            row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id=$1", user_id)
        tf = _load_training_form(row.get("training_form") if row else None) or {}
        plan_text = (tf.get("workout_text") or "").strip() or None
        plan_video = (tf.get("workout_video_url") or "").strip() or None
    except Exception:
        pass

    # собираем querystring безопасно
    params = {
        "mode": "workout",
        "shots": "3",
        "rest": str(rest_sec),
        "window": str(window_sec),
        "verify": "home",
    }
    if plan_text:
        params["plan_text"] = plan_text[:800]
    if plan_video:
        params["plan_video"] = plan_video[:500]

    url = _build_webapp_url(params)
    rows = [[KeyboardButton("▶️ Начать тренировку", web_app=WebAppInfo(url=url))]]

    rows.append([KeyboardButton("📊 Профиль")] if _is_registered(user_id) else [KeyboardButton("📝 Регистрация")])

    if _is_admin(user_id):
        rows.append([KeyboardButton("🟢 Старт тренировки (админ)"),
                     KeyboardButton("🔴 Стоп тренировки (админ)")])
        rows.append([KeyboardButton("/delete_db"), KeyboardButton("/clear_db")])
        rows.append([KeyboardButton("🧹 Очистить мои данные")])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def _make_keyboard(is_workout: bool, user_id: int) -> ReplyKeyboardMarkup:
    rows = []
    if is_workout:
        rest_sec = _get_rest_seconds_cached(user_id)
        window_sec = _get_window_seconds_cached(user_id)
        rows.append([
            KeyboardButton(
                "▶️ Начать тренировку",
                web_app=WebAppInfo(
                    url=_build_webapp_url({
                        "mode": "workout",
                        "shots": "3",
                        "rest": str(rest_sec),
                        "window": str(window_sec),
                        "verify": "home",
                    })
                )
            )

        ])
    if _is_registered(user_id):
        rows.append([KeyboardButton("📊 Профиль")])
    else:
        rows.append([KeyboardButton("📝 Регистрация")])

    if _is_admin(user_id):
        rows.append([KeyboardButton("🟢 Старт тренировки (админ)"),
                     KeyboardButton("🔴 Стоп тренировки (админ)")])
        rows.append([KeyboardButton("/delete_db"), KeyboardButton("/clear_db")])
        rows.append([KeyboardButton("🧹 Очистить мои данные")])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)
# Пути к локальным картинкам
ASSET_IMG_1 = Path("assets/onboarding/01_runner.png")
ASSET_IMG_2 = Path("assets/onboarding/02_icons.png")

async def _send_local_photo_or_text(bot, chat_id, img_path: Path, caption: str,
                                    parse_mode: str = "Markdown", reply_markup=None):
    """Если файл есть — отправим фото с подписью, иначе просто текст."""
    try:
        if img_path.exists():
            with img_path.open("rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f, caption=caption,
                                     parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=caption,
                                   parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        # На всякий случай fallback в текст
        await bot.send_message(chat_id=chat_id, text=caption,
                               parse_mode=parse_mode, reply_markup=reply_markup)

# ---------------- Инлайн-кнопки залога/депозита ----------------
def _deposit_complete_kb(chosen: str | None = None, locked: bool = False) -> InlineKeyboardMarkup:
    def btn(text: str, cb: str):
        mark = "✅ " if (chosen == cb) else ""
        data = "dep_locked" if locked else cb
        return InlineKeyboardButton(f"{mark}{text}", callback_data=data)
    rows = [
        [btn("🔁 Повторить заморозку", "depwin_repeat")],
        [btn("✏️ Изменить залог", "depwin_change_amount")],
        [btn("🗓 Изменить расписание", "depwin_change_sched")],
        [btn("✖️ Позже", "depwin_later")],
    ]
    return InlineKeyboardMarkup(rows)

def _deposit_forfeit_kb(chosen: Optional[str] = None, locked: bool = False) -> InlineKeyboardMarkup:
    def btn(text: str, cb: str):
        mark = "✅ " if (chosen == cb) else ""
        data = "dep_locked" if locked else cb
        return InlineKeyboardButton(f"{mark}{text}", callback_data=data)
    rows = [
        [btn("🔁 Начать заново",      "depforf_restart")],
        [btn("✏️ Изменить залог",     "depwin_change_amount")],
        [btn("🗓 Изменить расписание","depwin_change_sched")],
        [btn("✖️ Позже",              "depwin_later")],
    ]
    return InlineKeyboardMarkup(rows)

# ---------------- Прочие утилиты ----------------
def _h(x: Optional[str]) -> str:
    return html.escape(str(x)) if x is not None else ""

def days_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["пн ср пт", "вт чт сб", "пн-пт"],
            ["каждый день", "сб вс", "без расписания"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def time_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["07:00", "08:00", "18:00"],
            ["19:00", "19:30", "20:00"],
            ["Другое время"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def rest_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["30 сек", "60 сек", "90 сек"],
            ["120 сек", "180 сек"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def duration_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["30", "45", "60"],
            ["75", "90"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def _current_keyboard(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> ReplyKeyboardMarkup:
    active = bool(context.application.bot_data.get("session_active", {}).get(user_id))
    return _make_keyboard(active, user_id)

# ---------------- Парсеры ----------------
WEEKDAYS_MAP = {
    'пн': 'mon', 'пон': 'mon', 'понедельник': 'mon',
    'вт': 'tue', 'вторник': 'tue',
    'ср': 'wed', 'среда': 'wed',
    'чт': 'thu', 'четверг': 'thu',
    'пт': 'fri', 'пятница': 'fri',
    'сб': 'sat', 'суббота': 'sat',
    'вс': 'sun', 'воскресенье': 'sun'
}
ORDERED_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
RU_BY_EN = {'mon': 'пн', 'tue': 'вт', 'wed': 'ср', 'thu': 'чт', 'fri': 'пт', 'sat': 'сб', 'sun': 'вс'}

RU_FULL_TO_EN = {
    'понедельник': 'mon', 'вторник': 'tue', 'среда': 'wed',
    'четверг': 'thu', 'пятница': 'fri', 'суббота': 'sat', 'воскресенье': 'sun',
}
EN_TO_RU_FULL = {v: k.capitalize() for k, v in RU_FULL_TO_EN.items()}

def _parse_time_hhmm(s: str) -> Optional[time]:
    m = re.search(r'(\d{1,2})[:.](\d{2})', s or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh < 24 and 0 <= mm < 60:
        return time(hour=hh, minute=mm)
    return None

def _parse_days(s: str) -> List[str]:
    s = (s or "").strip().lower()
    if not s or s == "без расписания":
        return []
    if 'каждый день' in s or 'ежеднев' in s or 'все дни' in s or 'пн-вс' in s:
        return ORDERED_DAYS.copy()
    rng = re.search(r'(пн|пон|вт|ср|чт|пт|сб|вс)\s*-\s*(пн|пон|вт|ср|чт|пт|сб|вс)', s)
    if rng:
        a, b = WEEKDAYS_MAP[rng.group(1)], WEEKDAYS_MAP[rng.group(2)]
        ia, ib = ORDERED_DAYS.index(a), ORDERED_DAYS.index(b)
        return ORDERED_DAYS[ia:ib+1] if ia <= ib else ORDERED_DAYS[ia:]+ORDERED_DAYS[:ib+1]
    days = []
    for token in re.split(r'[,\s]+', s):
        token = token.strip()
        if token in WEEKDAYS_MAP:
            days.append(WEEKDAYS_MAP[token])
    seen = set()
    uniq = []
    for d in days:
        if d not in seen:
            uniq.append(d)
            seen.add(d)
    return uniq

def _human_days(days: List[str]) -> str:
    if not days:
        return "без расписания"
    return " ".join(RU_BY_EN.get(d, d) for d in days)

def _parse_rest_seconds(s: str) -> Optional[int]:
    s = (s or "").strip().lower()
    if not s:
        return None
    if re.match(r"^\d{1,2}[:.]\d{1,2}$", s):  # mm:ss
        mm, ss = re.split(r"[:.]", s)
        return int(mm) * 60 + int(ss)
    m = re.search(r"\d+", s)
    if not m:
        return None
    val = int(m.group(0))
    if "мин" in s:
        return max(1, val * 60)
    return max(1, val)

def _parse_duration_minutes(s: str) -> Optional[int]:
    s = (s or "").strip().lower()
    if not s:
        return None
    m = re.search(r"\d{1,3}", s)
    if not m:
        return None
    val = int(m.group(0))
    if not (5 <= val <= 240):
        return None
    return val

# ---------------- Хелперы расписания/форматирования ----------------
def _human_schedule_lines(per_day_time: Dict[str, str],
                          per_day_duration: Optional[Dict[str, int]] = None) -> List[str]:
    lines = []
    for d in ORDERED_DAYS:
        if d not in per_day_time:
            continue
        ru = EN_TO_RU_FULL.get(d, d)
        hhmm = per_day_time[d]
        if per_day_duration and d in per_day_duration:
            lines.append(f"• {ru} — {hhmm} × {per_day_duration[d]} мин")
        else:
            lines.append(f"• {ru} — {hhmm}")
    return lines

def _progress_bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "—"
    p = max(0, min(100, int(done * 100 / total)))
    filled = (p * width) // 100
    return "▰" * filled + "▱" * (width - filled)

def _add_minutes_to_time(t: time, minutes: int, tz: ZoneInfo) -> Tuple[time, int]:
    base = datetime.combine(date(2000, 1, 3), time(t.hour, t.minute, t.second, t.microsecond, tzinfo=tz))
    dt2 = base + timedelta(minutes=minutes)
    day_shift = (dt2.date() - base.date()).days
    return dt2.timetz(), day_shift

# ===== helper: безопасное чтение training_form из БД (str|dict) =====
def _load_training_form(tf_raw) -> Dict:
    if isinstance(tf_raw, dict):
        return tf_raw or {}
    if isinstance(tf_raw, str) and tf_raw.strip():
        try:
            return json.loads(tf_raw)
        except Exception:
            return {}
    return {}

def _format_deposit_status(tf: dict, tz: ZoneInfo) -> str:
    dep = tf.get("deposit")
    if dep is None:
        return "• Залог: —"

    dep = int(dep or 0)
    forfeited = bool(tf.get("deposit_forfeit"))
    left = int(tf.get("deposit_left") or 0)
    reason = (tf.get("deposit_forfeit_reason") or "").strip()
    forfeited_at = tf.get("deposit_forfeit_at")

    if forfeited:
        when = ""
        if forfeited_at:
            try:
                dt = datetime.fromisoformat(forfeited_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=dt_timezone.utc)
                when = dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
            except Exception:
                when = forfeited_at
        base = f"• Залог: списан {dep} ₽"
        if when:
            base += f" — {html.escape(when)}"
        if reason:
            base += f" (причина: {html.escape(reason)})"
        return base

    if left and left != dep:
        return f"• Залог: {dep} ₽ (осталось {left} ₽)"
    return f"• Залог: {dep} ₽ (на кону)"

# ---------------- Планировщик напоминаний ----------------
def _clear_user_jobs(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    jq = getattr(context.application, "job_queue", None)
    if jq:
        try:
            for job in jq.jobs():
                if (job.name or "").startswith(f"{user_id}:"):
                    job.schedule_removal()
        except Exception as e:
            logger.exception("Failed to list/remove jobs: %s", e)

async def clear_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.effective_message.reply_text("🚫 Доступ запрещён.",
            reply_markup=_current_keyboard(context, user.id))
        return
    try:
        async with Database.acquire() as conn:
            try:
                await conn.execute("DELETE FROM tasks WHERE user_id=$1", user.id)
            except Exception:
                pass
            try:
                await conn.execute("DELETE FROM sets  WHERE user_id=$1", user.id)
            except Exception:
                pass
            await conn.execute(
                """
                UPDATE users
                   SET reminder_enabled = FALSE,
                       reminder_days = ARRAY[]::text[],
                       reminder_time = NULL,
                       workout_duration = NULL,
                       rest_seconds = 60,
                       training_form = NULL
                 WHERE user_id = $1
                """,
                user.id
            )

        _clear_user_jobs(context, user.id)
        _set_session_active(context, user.id, False)
        REST_CACHE.pop(user.id, None)
        WORKOUT_WINDOW_CACHE.pop(user.id, None)

        await update.effective_message.reply_text(
            "✅ Твои данные очищены. Напоминания выключены.",
            reply_markup=_make_keyboard(False, user.id)
        )
    except Exception as e:
        logger.exception("clear_my_data failed: %s", e)
        await update.effective_message.reply_text(
            "⚠️ Ошибка при очистке твоих данных.",
            reply_markup=_make_keyboard(False, user.id)
        )

def _schedule_reminders_per_day(context: ContextTypes.DEFAULT_TYPE,
                                user_id: int,
                                per_day_time: Dict[str, str],
                                per_day_duration: Optional[Dict[str, int]] = None,
                                default_duration_min: int = 60) -> None:
    jq = getattr(context.application, "job_queue", None)
    if jq is None:
        logger.warning("JobQueue is not available; skipping reminders for user %s", user_id)
        return

    _clear_user_jobs(context, user_id)
    if not per_day_time:
        return

    tz = _tz_for(user_id)
    day_idx = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}

    for d in ORDERED_DAYS:
        if d not in per_day_time or d not in day_idx:
            continue

        hhmm = per_day_time[d]
        t = _parse_time_hhmm(hhmm)
        if not t:
            logger.warning("[sched] skip day=%s invalid time=%r", d, hhmm)
            continue
        t_z = time(t.hour, t.minute, t.second, t.microsecond, tzinfo=tz)

        dur = int((per_day_duration or {}).get(d, default_duration_min))
        if dur < 1:
            dur = default_duration_min

        mid_t, mid_shift = _add_minutes_to_time(t_z, max(dur // 2, 1), tz)
        end_t, end_shift = _add_minutes_to_time(t_z, dur, tz)

        base_day = (day_idx[d]+1) % 7
        mid_day = (base_day + mid_shift) % 7
        end_day = (base_day + end_shift) % 7

        async def start_cb(ctx: ContextTypes.DEFAULT_TYPE, uid=user_id):
            _set_session_active(ctx, uid, True)
            _ws_reset(ctx, uid)
            _ws_get(ctx, uid)

            dep_amt = 0
            try:
                async with Database.acquire() as conn:
                    row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id=$1", uid)
                tf = _load_training_form(row.get("training_form") if row else None) or {}
                if not tf.get("deposit_forfeit"):
                    left = int(tf.get("deposit_left") or 0)
                    dep_amt = left if left > 0 else int(tf.get("deposit") or 0)
            except Exception:
                dep_amt = 0

            intro_line = "🏁 Старт окна! Жми «▶️ Начать тренировку»."
            money_line = f"\n💸 На кону: {dep_amt} ₽. Начни в течение 5 минут, иначе деньги спишутся." if dep_amt > 0 else ""

            kb = await _build_workout_keyboard(ctx, uid)

            try:
                await ctx.bot.send_message(
                    chat_id=uid,
                    text=f"{intro_line}{money_line}\nБудет 3 снимка с паузами отдыха.",
                    reply_markup=kb
                )
            except Exception:
                logger.exception("Failed to send START reminder")

            jq = getattr(ctx.application, "job_queue", None)
            if jq:
                for job in jq.jobs():
                    if (job.name or "") == f"{uid}:nostart":
                        job.schedule_removal()

                async def _no_start_job(_ctx: ContextTypes.DEFAULT_TYPE) -> None:
                    try:
                        cur_ws = _ws_get(_ctx, uid)
                        started = len(cur_ws.get("results", [])) > 0
                        if (not started) and _is_session_active(_ctx, uid):
                            await _forfeit_deposit(_ctx, uid, "не начал тренировку в течение 5 минут")
                            _set_session_active(_ctx, uid, False)
                    except Exception as e:
                        logger.exception("_no_start_job failed: %s", e)

                jq.run_once(_no_start_job, when=timedelta(minutes=5), name=f"{uid}:nostart")

        async def mid_cb(ctx: ContextTypes.DEFAULT_TYPE, uid=user_id):
            _set_session_active(ctx, uid, True)
            try:
                await ctx.bot.send_message(
                    chat_id=uid,
                    text="⏳ Середина — держи темп. Если ещё не начал, жми «▶️ Начать тренировку».",
                    reply_markup=_make_keyboard(True, uid)
                )
            except Exception:
                logger.exception("Failed to send MID reminder")

        async def end_cb(ctx: ContextTypes.DEFAULT_TYPE, uid=user_id):
            _set_session_active(ctx, uid, False)
            try:
                await ctx.bot.send_message(
                    chat_id=uid,
                    text="✅ Конец тренировочного окна.",
                    reply_markup=_make_keyboard(False, uid)
                )
            except Exception:
                logger.exception("Failed to send END reminder")

        jq.run_daily(start_cb, time=t_z,   days=(base_day,), name=f"{user_id}:{d}:start")
        jq.run_daily(mid_cb,   time=mid_t, days=(mid_day,),  name=f"{user_id}:{d}:mid")
        jq.run_daily(end_cb,   time=end_t, days=(end_day,),  name=f"{user_id}:{d}:end")

# ---------------- Помощники сессии ----------------
def _set_session_active(context: ContextTypes.DEFAULT_TYPE, user_id: int, active: bool) -> None:
    sa = context.application.bot_data.setdefault("session_active", {})
    if active:
        sa[user_id] = True
    else:
        sa.pop(user_id, None)

def _is_session_active(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return bool(context.application.bot_data.get("session_active", {}).get(user_id))

async def _reschedule_from_db(update_or_context, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    try:
        async with Database.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT reminder_enabled, reminder_days, reminder_time, workout_duration, rest_seconds, training_form, timezone
                  FROM users
                 WHERE user_id = $1
                """,
                user_id
            )
        if not row:
            return

        tz_name = row.get("timezone") or getattr(settings, "TIMEZONE", "Europe/Moscow")
        _set_tz_for(user_id, tz_name)

        _set_rest_seconds_cached(user_id, int((row.get("rest_seconds") or 60)))

        if not row["reminder_enabled"]:
            _clear_user_jobs(context, user_id)
            _set_session_active(context, user_id, False)
            return

        default_dur = int(row.get("workout_duration") or 60)
        _set_window_seconds_cached(user_id, default_dur * 60)

        tf = _load_training_form(row.get("training_form"))
        per_day_time = tf.get("per_day_time") or {}
        per_day_duration = tf.get("per_day_duration") or None

        if per_day_time:
            _schedule_reminders_per_day(context, user_id, per_day_time, per_day_duration, default_duration_min=default_dur)
        _set_registered(user_id, bool(per_day_time))
    except Exception as e:
        logger.exception("_reschedule_from_db failed: %s", e)

# ---------------- Залог: списание ----------------
async def _forfeit_deposit(context: ContextTypes.DEFAULT_TYPE, user_id: int, reason: str) -> None:
    try:
        async with Database.acquire() as conn:
            row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id=$1", user_id)
        tf = _load_training_form(row.get("training_form") if row else None) or {}

        if tf.get("deposit_forfeit"):
            return

        deposit = int(tf.get("deposit") or 0)
        if deposit <= 0:
            return

        tf["deposit_forfeit"] = True
        tf["deposit_forfeit_reason"] = str(reason)
        tf["deposit_forfeit_at"] = datetime.now(_tz_for(user_id)).isoformat()
        tf["deposit_left"] = 0

        async with Database.acquire() as conn:
            await conn.execute(
                "UPDATE users SET training_form=$2 WHERE user_id=$1",
                user_id, json.dumps(tf, ensure_ascii=False)
            )

        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ Залог {deposit} ₽ списан: {reason}"
        )
    except Exception as e:
        logger.exception("_forfeit_deposit failed: %s", e)

# ===================== AI-залог =====================
try:
    from gpt_tasks import recommend_deposit_with_gpt  # type: ignore
except Exception:
    recommend_deposit_with_gpt = None  # fallback ниже

def _clamp_deposit(v: int) -> int:
    return max(500, min(int(v), 100_000))

def _build_onboarding_profile(user, st: dict) -> dict:
    per_day_time = st.get("schedule_map_time") or {}
    per_day_duration = st.get("schedule_map_duration") or {}
    dur_common = st.get("duration_common_min")
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
        },
        "answers": st.get("answers") or {},  # ← новые ответы (3 вопроса)
        "schedule": {
            "per_day_time": per_day_time,
            "per_day_duration": per_day_duration if per_day_duration else None,
            "duration_common_min": dur_common,
        },
        "rest_seconds": st.get("rest_seconds"),
        "program_price": st.get("program_price"),  # если где-то попадётся
    }

async def _ai_recommend_deposit(user, st: dict) -> tuple[int, str]:
    profile = _build_onboarding_profile(user, st)

    if callable(recommend_deposit_with_gpt):
        try:
            resp = await recommend_deposit_with_gpt(profile)  # {"deposit": int, "reason": str}
            dep = _clamp_deposit(int(resp.get("deposit", 5000)))
            reason = str(resp.get("reason") or "ИИ-рекомендация по анкете")
            return dep, reason
        except Exception:
            pass

    # Fallback-эвристика — без привязки к старым полям
    dep = 5000
    per_day_time = (profile.get("schedule") or {}).get("per_day_time") or {}
    days_cnt = len(per_day_time)
    if days_cnt >= 4:
        dep += 1000
    if days_cnt >= 6:
        dep += 1000

    dur_common = (profile.get("schedule") or {}).get("duration_common_min")
    per_day_duration = (profile.get("schedule") or {}).get("per_day_duration") or {}
    avg_dur = None
    try:
        if dur_common:
            avg_dur = int(dur_common)
        elif per_day_duration:
            vals = [int(x) for x in per_day_duration.values() if x]
            if vals:
                avg_dur = sum(vals)//len(vals)
    except Exception:
        pass
    if avg_dur and avg_dur >= 60:
        dep += 1000

    # если в ответах встречаются крупные числа — трактуем как «готовность/деньги/цена»
    try:
        answers_text = " ".join(str(v) for v in (profile.get("answers") or {}).values())
        m = re.search(r"\d{3,6}", answers_text.replace(" ", ""))
        if m and int(m.group(0)) >= 5000:
            dep += 1500
    except Exception:
        pass

    dep = _clamp_deposit(dep)
    return dep, "Резервная эвристика (ИИ недоступен)"

async def _auto_deposit_and_finish(message: Message, update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict):
    user = update.effective_user
    wait_msg = await message.reply_text("🤖 Считаю рекомендуемый залог по твоим ответам…")
    dep, why = await _ai_recommend_deposit(user, st)
    st["deposit"] = dep  # дефолт — рекомендация

    txt = f"🧮 Рекомендуемый залог: *{dep} ₽*\nПричина: {why}\n\nВыбери:"
    try:
        await wait_msg.edit_text(txt, parse_mode="Markdown", reply_markup=_deposit_choice_kb(dep))
    except Exception:
        await message.reply_text(txt, parse_mode="Markdown", reply_markup=_deposit_choice_kb(dep))

    st["step"] = "deposit_choice"

# ===================== КОСМЕТИКА РЕГИСТРАЦИИ (инлайн) =====================
DAY_LABELS = [
    ("mon", "Пн"), ("tue", "Вт"), ("wed", "Ср"),
    ("thu", "Чт"), ("fri", "Пт"), ("sat", "Сб"), ("sun", "Вс"),
]
EN2RU_SHORT = dict(DAY_LABELS)
RU_FULL_BY_EN = EN_TO_RU_FULL

TIME_PRESETS = ["07:00", "08:00", "18:00", "19:00", "19:30", "20:00"]
DUR_PRESETS = [30, 45, 60, 75, 90]
REST_PRESETS = [30, 60, 90, 120, 180]

def _days_toggle_kb(st: dict) -> InlineKeyboardMarkup:
    chosen = set(st.get("chosen_days", []))
    rows = []
    buf = []
    for i, (key, label) in enumerate(DAY_LABELS, 1):
        mark = "✅ " if key in chosen else ""
        buf.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"days_toggle:{key}"))
        if i % 3 == 0:
            rows.append(buf); buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton("🧹 Сбросить", callback_data="days_clear"),
                 InlineKeyboardButton("Готово ▶️", callback_data="days_done")])
    return InlineKeyboardMarkup(rows)

def _time_kb_for_day(day_en: str, current: Optional[str] = None) -> InlineKeyboardMarkup:
    rows = []
    buf = []
    for i, t in enumerate(TIME_PRESETS, 1):
        mark = "✅ " if current == t else ""
        buf.append(InlineKeyboardButton(f"{mark}{t}", callback_data=f"time_pick:{day_en}:{t}"))
        if i % 3 == 0:
            rows.append(buf); buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton("⌨️ Другое время", callback_data=f"time_custom:{day_en}")])
    return InlineKeyboardMarkup(rows)

def _rest_inline_kb() -> InlineKeyboardMarkup:
    rows = []
    buf = []
    for i, v in enumerate(REST_PRESETS, 1):
        label = f"{v//60}:{v%60:02d}" if v >= 60 else f"{v}с"
        buf.append(InlineKeyboardButton(label, callback_data=f"rest:{v}"))
        if i % 3 == 0:
            rows.append(buf); buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton("⌨️ Другое", callback_data="rest_custom")])
    return InlineKeyboardMarkup(rows)

def _dur_mode_inline_kb_pretty() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Одинаковая длительность", callback_data="dur_same")],
        [InlineKeyboardButton("Разная по дням", callback_data="dur_diff")],
    ])

def _deposit_choice_kb(dep: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"👍 Согласен с {dep} ₽", callback_data="dep_ok")],
        [InlineKeyboardButton("✏️ Ввести свою сумму", callback_data="dep_custom")],
    ])

def _dur_common_kb(current: int = 60) -> InlineKeyboardMarkup:
    rows = []
    buf = []
    for i, v in enumerate(DUR_PRESETS, 1):
        mark = "✅ " if v == current else ""
        buf.append(InlineKeyboardButton(f"{mark}{v} мин", callback_data=f"dur_common_set:{v}"))
        if i % 3 == 0:
            rows.append(buf); buf = []
    if buf:
        rows.append(buf)
    rows.append([
        InlineKeyboardButton("−5", callback_data="dur_common_adj:-5"),
        InlineKeyboardButton("−1", callback_data="dur_common_adj:-1"),
        InlineKeyboardButton("+1", callback_data="dur_common_adj:+1"),
        InlineKeyboardButton("+5", callback_data="dur_common_adj:+5"),
    ])
    rows.append([InlineKeyboardButton("⌨️ Другое (ввести)", callback_data="dur_common_custom"),
                 InlineKeyboardButton("Готово ▶️", callback_data="dur_common_done")])
    return InlineKeyboardMarkup(rows)

def _dur_perday_kb(day_en: str, current: int = 60) -> InlineKeyboardMarkup:
    rows = []
    buf = []
    for i, v in enumerate(DUR_PRESETS, 1):
        mark = "✅ " if v == current else ""
        buf.append(InlineKeyboardButton(f"{mark}{v} мин", callback_data=f"dur_pd_set:{day_en}:{v}"))
        if i % 3 == 0:
            rows.append(buf); buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton("⌨️ Другое", callback_data=f"dur_pd_custom:{day_en}")])
    return InlineKeyboardMarkup(rows)

# ===================== НОВАЯ РЕГИСТРАЦИЯ (с 2 фото и 3 вопросами) =====================
ONBOARDING_TEXT_1 = (
    "Я — Foscar, твой личный тренер и строгий напарник 🥷.\n\n"
    "Сейчас ты в состоянии *неосознанного оптимизма*. "
    "Мотивация спадёт — я удержу тебя в колее ⚡️"
)

ONBOARDING_TEXT_2 = (
    "🔥 {name}, настало время для первого шага.\n\n"
    "✨ Чтобы я мог вести тебя максимально эффективно, мне нужно немного узнать о тебе. "
    "Всего 3 коротких вопроса — и ты поможешь себе выстроить прочный фундамент для дисциплины и результата.\n\n"
    "🎯 Поймём, что действительно тобой движет.\n"
    "🛡 Определим твои сильные и слабые стороны.\n"
    "💰 Найдём сумму залога, которая будет держать тебя в игре.\n\n"
    "⚔️ Отвечая честно, ты помогаешь самому себе. Я не дам тебе свернуть с пути.\n\n"
    "👇 Готов? Нажми кнопку, и начнём."
)

def _reg_questions() -> List[str]:
    # Можно переопределить в settings.ONBOARDING_QUESTIONS = ["...", "...", "..."]
    qs = getattr(settings, "ONBOARDING_QUESTIONS", None)
    if isinstance(qs, (list, tuple)) and len(qs) >= 3:
        return [str(qs[0]), str(qs[1]), str(qs[2])]
    # Дефолт — нейтральные формулировки
    return [
        "1) Почему ты начинаешь сейчас? Что для тебя важно?",
        "2) Какая конкретная цель на ближайшие 4 недели (измеримая)?",
        "3) Что тебя чаще всего срывает и как мы это обойдём?",
    ]

def _reg_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("reg", {})

def _reg_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return "reg" in context.user_data

async def _already_registered(user_id: int) -> bool:
    async with Database.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT training_form FROM users WHERE user_id = $1",
            user_id
        )
    tf = _load_training_form(row.get("training_form") if row else None)
    per_day_time = (tf or {}).get("per_day_time") or {}
    return bool(per_day_time)

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user

    if await _already_registered(user.id):
        await msg.reply_text(
            "Ты уже прошёл онбординг — повторная регистрация не нужна.\n",
            reply_markup=_make_keyboard(False, user.id)
        )
        return

    # создаём пользователя при необходимости (как у тебя было)
    async with Database.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, rest_seconds, timezone FROM users WHERE user_id=$1", user.id)
        if not row:
            await conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, timezone)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (user_id) DO NOTHING
                """,
                user.id, user.username, user.first_name, user.last_name,
                getattr(settings, "TIMEZONE", "Europe/Moscow")
            )
            _set_rest_seconds_cached(user.id, 60)
            _set_tz_for(user.id, getattr(settings, "TIMEZONE", "Europe/Moscow"))
        else:
            _set_rest_seconds_cached(user.id, int(row.get("rest_seconds") or 60))
            _set_tz_for(user.id, row.get("timezone") or getattr(settings, "TIMEZONE", "Europe/Moscow"))

    st = _reg_state(context)
    st.clear()
    st["name"] = user.first_name or (user.username and f"@{user.username}") or "друг"
    st["step"] = "await_qa_begin"
    st["answers"] = {}
    st["schedule_map_time"] = {}
    st["schedule_map_duration"] = {}

    # пин сверху
    pinned = await msg.reply_text("🔥🔥🔥\n*ПОМНИ СВОЮ ЦЕЛЬ*\n🔥🔥🔥", parse_mode="Markdown")
    try:
        await context.bot.pin_chat_message(chat_id=msg.chat_id, message_id=pinned.message_id)
    except Exception:
        pass

    # Экран №1 — ТОЛЬКО он + кнопка «Дальше»
    kb1 = InlineKeyboardMarkup([[InlineKeyboardButton("Дальше ▶️", callback_data="ob_next")]])
    await _send_local_photo_or_text(
        context.bot, msg.chat_id, ASSET_IMG_1, ONBOARDING_TEXT_1,
        parse_mode="Markdown", reply_markup=kb1
    )

# ===== Стартовая ресинхронизация напоминаний для всех пользователей =====
from types import SimpleNamespace

async def reschedule_all_users(app) -> None:
    """Поднять все run_daily задачи из БД после рестарта процесса."""
    try:
        async with Database.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, timezone, rest_seconds, workout_duration,
                       training_form, reminder_enabled
                  FROM users
                 WHERE reminder_enabled = TRUE
            """)
        for r in rows:
            uid = int(r["user_id"])
            tz_name = r.get("timezone") or getattr(settings, "TIMEZONE", "Europe/Moscow")
            _set_tz_for(uid, tz_name)
            _set_rest_seconds_cached(uid, int(r.get("rest_seconds") or 60))

            tf = _load_training_form(r.get("training_form"))
            per_day_time = (tf.get("per_day_time") or {})
            per_day_duration = (tf.get("per_day_duration") or None)

            default_dur = int(
                r.get("workout_duration")
                or (next(iter(per_day_duration.values())) if per_day_duration else 60)
            )
            _set_window_seconds_cached(uid, default_dur * 60)

            if per_day_time:
                # делаем «псевдо-context», потому что _schedule_reminders_per_day ждёт context.application.job_queue
                fake_ctx = SimpleNamespace(
            application=SimpleNamespace(job_queue=app.job_queue),
                    bot=app.bot,
                )
                _schedule_reminders_per_day(
                    fake_ctx, uid, per_day_time, per_day_duration,
                    default_duration_min=default_dur
                )
                _set_registered(uid, True)
                logger.info("[startup] rescheduled user=%s days=%s",
                            uid, list(per_day_time.keys()))
    except Exception as e:
        logger.exception("reschedule_all_users failed: %s", e)

async def register_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # if not _reg_active(context):
    #     return
    # st = _reg_state(context)
    # step = st.get("step")
    #
    # if step not in ("photo1", "photo2"):
    #     return
    #
    # msg = update.effective_message
    # file_id = _extract_image_file_id_from_message(update.message)
    # if not file_id:
    #     await msg.reply_text("Это не похоже на фото. Пришли фото как изображение 🙏")
    #     return
    #
    # # сохраняем id, чтобы потом (опционально) утянуть из Telegram
    # st["photos"].append(file_id)
    #
    # # фото №1 -> отправляем Текст 1 с фото + просим фото №2
    # if step == "photo1":
    #     try:
    #         await context.bot.send_photo(
    #             chat_id=msg.chat_id,
    #             photo=file_id,
    #             caption=ONBOARDING_TEXT_1,
    #             parse_mode="Markdown"
    #         )
    #     except Exception:
    #         await msg.reply_text(ONBOARDING_TEXT_1, parse_mode="Markdown")
    #
    #     st["step"] = "photo2"
    #     await msg.reply_text("📷 Отлично. Теперь пришли *второе* фото (№2) — я приложу его к тексту #2.", parse_mode="Markdown")
    #     return
    #
    # # фото №2 -> отправляем Текст 2 с фото + кнопка "Начать 3 вопроса"
    # if step == "photo2":
    #     name = st.get("name", "друг")
    #     text2 = ONBOARDING_TEXT_2.format(name=name)
    #     try:
    #         await context.bot.send_photo(
    #             chat_id=msg.chat_id,
    #             photo=file_id,
    #             caption=text2,
    #             parse_mode="Markdown",
    #             reply_markup=InlineKeyboardMarkup([
    #                 [InlineKeyboardButton("▶️ Начать 3 вопроса", callback_data="qa_begin")]
    #             ])
    #         )
    #     except Exception:
    #         await msg.reply_text(
    #             text2,
    #             parse_mode="Markdown",
    #             reply_markup=InlineKeyboardMarkup([
    #                 [InlineKeyboardButton("▶️ Начать 3 вопроса", callback_data="qa_begin")]
    #             ])
    #         )
    #     st["step"] = "await_qa_begin"
        return

async def register_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _reg_active(context):
        return

    msg = update.effective_message
    text = (msg.text or "").strip()
    st = _reg_state(context)

    # ───────────────── 3 ВОПРОСА ─────────────────
    if st.get("step") in ("q1", "q2", "q3"):
        answers = st.setdefault("answers", {})
        if st["step"] == "q1":
            answers["q1"] = text
            st["step"] = "q2"
            await msg.reply_text(_reg_questions()[1])
            return

        if st["step"] == "q2":
            answers["q2"] = text
            st["step"] = "q3"
            await msg.reply_text(_reg_questions()[2])
            return

        if st["step"] == "q3":
            answers["q3"] = text
            # Подстрахуемся: вытащим возможную сумму из ответа; GPT позже может её переопределить.
            st["deposit"] = _parse_deposit_from_text(text)
            # Перед расписанием спросим план тренировки (ссылка или текст)
            st["step"] = "q_plan"
            await msg.reply_text(
                "📹 Опиши тренировку:\n"
                "— напиши текст (что делаешь),\n"
                "— или пришли ССЫЛКУ на видео (YouTube/VK и т.п.).\n\n"
                "Примеры:\n"
                "• \"Разминка 5 мин, 3×10 отжиманий, 3×15 приседаний...\"\n"
                "• https://youtu.be/XXXXX",
            )
            return

    # ───────────────── ПЛАН ТРЕНИРОВКИ (текст/ссылка) ─────────────────
    if st.get("step") == "q_plan":
        url_m = re.search(r'(https?://\S+)', text)
        if url_m:
            st["workout_video_url"] = url_m.group(1).strip()
            st["workout_text"] = None
        else:
            st["workout_text"] = text.strip()[:2000] if text.strip() else None
            st["workout_video_url"] = None

        # Переходим к выбору дней (тумблеры)
        st["step"] = "pick_days"
        st["chosen_days"] = []
        await msg.reply_text(
            "🗓 Выбери дни тренировок (нажимай, чтобы включать/выключать). Потом — «Готово ▶️».",
            reply_markup=_days_toggle_kb(st)
        )
        return

    # ───────────────── Ручной ввод времени для дня ─────────────────
    if st.get("temp_day_en") and st.get("step") in ("enter_time_for_day", "times_loop"):
        t = _parse_time_hhmm(text.replace(" ", "").replace(".", ":"))
        if not t:
            await msg.reply_text("Формат времени ЧЧ:ММ, напр. 18:00. Попробуй ещё раз.")
            return
        day_en = st.pop("temp_day_en")
        st["schedule_map_time"][day_en] = t.strftime("%H:%M")
        pend = st.get("pending_days_time", [])
        if pend:
            next_day = pend.pop(0)
            st["temp_day_en"] = next_day
            ru = RU_FULL_BY_EN.get(next_day, next_day)
            await msg.reply_text(
                f"⏰ Время для {ru}:",
                reply_markup=_time_kb_for_day(next_day, st["schedule_map_time"].get(next_day))
            )
            return
        st["step"] = "ask_rest_inline"
        await msg.reply_text("⏱️ Выбери отдых между подходами:", reply_markup=_rest_inline_kb())
        return

    # ───────────────── Ручной ввод отдыха ─────────────────
    if st.get("step") == "ask_rest":
        rest_sec = _parse_rest_seconds(text)
        if rest_sec is None or rest_sec > 24*60*60:
            await msg.reply_text("Введи секунды или ММ:СС. Пример: 60 или 1:30.")
            return
        st["rest_seconds"] = rest_sec
        st["step"] = "ask_duration_mode"
        await msg.reply_text("⏲️ Длительность одинаковая или разная по дням?", reply_markup=_dur_mode_inline_kb_pretty())
        return

    # ───────────────── Пользователь ввёл СВОЮ сумму залога ─────────────────
    if st.get("step") == "ask_deposit_custom":
        m = re.search(r"\d{1,6}", text.replace(" ", ""))
        if not m:
            await msg.reply_text("Нужно целое число в ₽. Пример: 3000")
            return
        val = _clamp_deposit(int(m.group(0)))
        st["deposit"] = val
        st["step"] = "ask_deposit_days"
        st.setdefault("deposit_started_at", date.today().isoformat())
        st.setdefault("deposit_done_dates", [])
        await msg.reply_text("📅 На сколько дней заморозить залог? Введи число (1–90), например 7, 14, 21.")
        return

    # ───────────────── Ввод срока заморозки и завершение онбординга ─────────────────
    if st.get("step") == "ask_deposit_days":
        m = re.search(r"\d{1,3}", text)
        if not m:
            await msg.reply_text("Введи просто число дней (например 7). Диапазон 1–90.")
            return
        days = int(m.group(0))
        if not (1 <= days <= 90):
            await msg.reply_text("Число вне диапазона. Разрешено 1–90 дней.")
            return
        st["deposit_days"] = days

        await _reg_finish(msg, st)
        save_text = await _persist_onboarding_schedule_per_day(update.effective_user.id, context, st)
        if save_text:
            await msg.reply_text(save_text)

        context.user_data.pop("reg", None)
        await msg.reply_text(
            "Готово! Ниже — главное меню.",
            reply_markup=_make_keyboard(False, update.effective_user.id)
        )
        return

    # ───────────────── «⌨️ Другое (ввести)» — ОБЩАЯ длительность ─────────────────
    if st.get("step") == "ask_duration_common":
        dur = _parse_duration_minutes(text)
        if dur is None:
            await msg.reply_text("Введи минуты 5–240, например 60.")
            return
        st["duration_common_min"] = dur
        _set_window_seconds_cached(update.effective_user.id, int(dur) * 60)
        # ⬇️ СРАЗУ запускаем ИИ-рекомендацию залога + кнопки «Согласен / Своя сумма»
        await _auto_deposit_and_finish(msg, update, context, st)
        return

    # ───────────────── «⌨️ Другое» длительность для КОНКРЕТНОГО дня ─────────────────
    if st.get("step") == "ask_duration_for_day_custom" and st.get("temp_day_en"):
        dur = _parse_duration_minutes(text)
        if dur is None:
            await msg.reply_text("Введи минуты 5–240 для этого дня.")
            return
        day_en = st.pop("temp_day_en")
        st.setdefault("schedule_map_duration", {})[day_en] = dur
        pend = st.get("pending_days_dur", [])
        if pend:
            next_day = pend.pop(0)
            st["temp_day_en"] = next_day
            ru = RU_FULL_BY_EN.get(next_day, next_day)
            await msg.reply_text(f"⏲️ Минуты для {ru}:", reply_markup=_dur_perday_kb(next_day, 60))
            return
        try:
            any_dur = next(iter(st.get("schedule_map_duration", {}).values()))
        except Exception:
            any_dur = 60
        _set_window_seconds_cached(update.effective_user.id, int(any_dur)*60)
        # ⬇️ ИИ-рекомендация залога
        await _auto_deposit_and_finish(msg, update, context, st)
        return

    # ───────────────── Фолбэк: просим пользоваться инлайн-кнопками ─────────────────
    if st.get("step") in ("pick_day", "pick_day_or_done", "pick_days"):
        await msg.reply_text(
            "Выбирай дни кнопками ниже и жми «Готово ▶️».",
            reply_markup=_days_toggle_kb(st)
        )


# ---------------- Сохранение настроек онбординга ----------------
async def _persist_onboarding_schedule_per_day(user_id: int, context: ContextTypes.DEFAULT_TYPE, st: dict) -> Optional[str]:
    per_day_time: Dict[str, str] = st.get("schedule_map_time") or {}
    if not per_day_time:
        return None

    per_day_time = {d: per_day_time[d] for d in ORDERED_DAYS if d in per_day_time}

    dur_mode = st.get("dur_mode")  # "same" | "per_day"
    per_day_duration: Dict[str, int] = {}

    if dur_mode == "per_day":
        raw = st.get("schedule_map_duration") or {}
        per_day_duration = {d: int(raw.get(d) or 60) for d in per_day_time.keys()}
        default_duration = 60
        workout_duration_common = None
    else:
        default_duration = int(st.get("duration_common_min") or 60)
        per_day_duration = {d: default_duration for d in per_day_time.keys()}
        workout_duration_common = default_duration

    rest_seconds = int(st.get("rest_seconds") or 60)
    _set_rest_seconds_cached(user_id, rest_seconds)

    dur_for_window = workout_duration_common
    if dur_for_window is None:
        try:
            dur_for_window = int(next(iter(per_day_duration.values())))
        except StopIteration:
            dur_for_window = 60
    _set_window_seconds_cached(user_id, int(dur_for_window) * 60)

    first_time_val: Optional[str] = None
    for d in ORDERED_DAYS:
        if d in per_day_time:
            first_time_val = per_day_time[d]
            break
    rtime: Optional[time] = _parse_time_hhmm(first_time_val) if first_time_val else None

    reminder_days = list(per_day_time.keys())

    extras = {
        "answers": st.get("answers") or {},             # новые ответы (3 вопроса)
        "deposit": st.get("deposit"),
        "deposit_days": st.get("deposit_days"),
        "deposit_started_at": st.get("deposit_started_at"),
        "deposit_done_dates": st.get("deposit_done_dates", []),
        "reg_photos": list(st.get("photos") or []),     # сохраняем 2 фото регистрации
        "workout_text": st.get("workout_text"),
        "workout_video_url": st.get("workout_video_url"),
    }

    training_form = {
        "per_day_time": per_day_time,
        "per_day_duration": per_day_duration,
        **extras,
    }
    training_form_json = json.dumps(training_form, ensure_ascii=False)

    async with Database.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
               SET reminder_enabled = TRUE,
                   reminder_days = $2,
                   reminder_time = $3,
                   workout_duration = $4,
                   rest_seconds = $5,
                   training_form = $6
             WHERE user_id = $1
            """,
            user_id,
            reminder_days,
            rtime,
            workout_duration_common,   # None если разная длительность
            rest_seconds,
            training_form_json
        )

    _schedule_reminders_per_day(
        context, user_id,
        per_day_time,
        per_day_duration,
        default_duration_min=(workout_duration_common or 60)
    )
    _set_registered(user_id, True)
    lines = _human_schedule_lines(per_day_time, per_day_duration)
    txt = "✅ Напоминания включены.\n" + "\n".join(lines) + f"\nОтдых: {rest_seconds} сек."
    return txt

def _reg_schedule_text_lines(st: dict) -> str:
    per_day_time: Dict[str, str] = st.get("schedule_map_time") or {}
    lines = _human_schedule_lines(per_day_time)
    return "\n".join(lines) if lines else "— (пока не указал)"

async def _reg_finish(msg: Message, st: dict):
    name = st.get("name") or "друг"
    dep = st.get("deposit", 500)
    deposit_days = int(st.get("deposit_days") or 7)
    schedule = _reg_schedule_text_lines(st)
    rest_seconds = int(st.get("rest_seconds") or 60)
    await msg.reply_text(
        f"🚀 Отлично, {name}! Мы замораживаем {dep} ₽ на {deposit_days} дн.\n\n"
        "Если выполнишь все тренировки — деньги полностью вернутся ✅\n\n"
        "Если пропустишь — потеряешь деньги\n"
        f"Твоё расписание:\n{schedule}\n"
        f"Отдых между подходами: {rest_seconds} сек."
    )

# ---------------- Инлайн-колбэки регистрации ----------------
async def register_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _reg_active(context):
        await update.callback_query.answer()
        return

    cq = update.callback_query
    data = cq.data or ""
    st = _reg_state(context)
    if not st and data in ("ob_next", "qa_begin"):
        user = update.effective_user
        st["name"] = user.first_name or (user.username and f"@{user.username}") or "друг"
        st["step"] = "await_qa_begin"
        st["answers"] = {}
        st["schedule_map_time"] = {}
        st["schedule_map_duration"] = {}

    # Если это вообще не наш онбординг — вежливо отвечаем и выходим
    if not _reg_active(context) and data not in ("ob_next", "qa_begin"):
        await _safe_cq_answer(cq)
        return

    await _safe_cq_answer(cq)

    # старт 3 вопросов
    if data == "qa_begin":
        st["step"] = "q1"
        await cq.message.reply_text(_reg_questions()[0])
        await _safe_cq_answer(cq)
        return
    # переход с экрана №1 на экран №2
    if data == "ob_next":
        # уберём клавиатуру у первого сообщения, чтобы кнопку не жали повторно
        try:
            await _safe_edit_reply_markup(cq.message, None)
        except Exception:
            pass

        text2 = ONBOARDING_TEXT_2.format(name=st.get("name", "друг"))
        kb2 = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Начать 3 вопроса", callback_data="qa_begin")]])
        await _send_local_photo_or_text(
            context.bot, cq.message.chat_id, ASSET_IMG_2, text2,
            parse_mode="Markdown", reply_markup=kb2
        )
        await _safe_cq_answer(cq)
        return

    # ====== Выбор дней (тумблеры) ======
    if data.startswith("days_toggle:"):
        day_en = data.split(":", 1)[1]
        chosen = set(st.get("chosen_days", []))
        if day_en in chosen:
            chosen.remove(day_en)
        else:
            chosen.add(day_en)
        st["chosen_days"] = list(chosen)
        await _safe_edit_reply_markup(cq.message, _days_toggle_kb(st))
        await _safe_cq_answer(cq)
        return

    if data == "days_clear":
        st["chosen_days"] = []
        await _safe_edit_reply_markup(cq.message, _days_toggle_kb(st))
        await _safe_cq_answer(cq, "Сброшено")
        return

    if data == "days_done":
        chosen = [d for d in ORDERED_DAYS if d in set(st.get("chosen_days", []))]
        if not chosen:
            await _safe_cq_answer(cq, "Выбери хотя бы один день", show_alert=True)
            return
        st["schedule_map_time"] = {}
        st["pending_days_time"] = chosen.copy()
        st["step"] = "times_loop"
        next_day = st["pending_days_time"].pop(0)
        st["temp_day_en"] = next_day
        ru = RU_FULL_BY_EN.get(next_day, next_day)
        await cq.message.reply_text(
            f"⏰ Время для {ru}:",
            reply_markup=_time_kb_for_day(next_day)
        )
        await _safe_cq_answer(cq)
        return

    # ====== Время по дням ======
    if data.startswith("time_pick:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            await _safe_cq_answer("Некорректные данные времени", show_alert=True)
            return
        _, day_en, hhmm = parts
        st.setdefault("schedule_map_time", {})[day_en] = hhmm
        pend = st.get("pending_days_time", [])
        if pend:
            nd = pend.pop(0)
            st["temp_day_en"] = nd
            ru = RU_FULL_BY_EN.get(nd, nd)
            await cq.message.reply_text(f"⏰ Время для {ru}:", reply_markup=_time_kb_for_day(nd))
            await _safe_cq_answer(cq, f"{EN2RU_SHORT.get(day_en, day_en)} — {hhmm}")
            return
        st.pop("temp_day_en", None)
        st["step"] = "ask_rest_inline"
        await cq.message.reply_text("⏱️ Выбери отдых между подходами:", reply_markup=_rest_inline_kb())
        await _safe_cq_answer(cq, f"{EN2RU_SHORT.get(day_en, day_en)} — {hhmm}")
        return

    if data.startswith("time_custom:"):
        _, day_en = data.split(":")
        st["temp_day_en"] = day_en
        st["step"] = "enter_time_for_day"
        ru = RU_FULL_BY_EN.get(day_en, day_en)
        await cq.message.reply_text(f"Введи время для {ru} в формате ЧЧ:ММ")
        await _safe_cq_answer(cq)
        return

    # ====== Отдых ======
    if data.startswith("rest:"):
        rest_sec = int(data.split(":", 1)[1])
        st["rest_seconds"] = rest_sec
        st["step"] = "ask_duration_mode"
        await cq.message.reply_text("⏲️ Длительность одинаковая или разная по дням?", reply_markup=_dur_mode_inline_kb_pretty())
        await _safe_cq_answer(cq, f"Отдых: {rest_sec} сек")
        return

    if data == "rest_custom":
        st["step"] = "ask_rest"
        await cq.message.reply_text("Введи отдых: секунды или ММ:СС (например 60 или 1:30).")
        await _safe_cq_answer(cq)
        return

    # ====== Режим длительности ======
    if data in ("dur_same", "dur_diff"):
        if data == "dur_same":
            st["dur_mode"] = "same"
            st["duration_common_min"] = int(st.get("duration_common_min") or 60)
            st["step"] = "ask_duration_common_inline"
            await cq.message.reply_text(
                "⏲️ Минуты на все дни:",
                reply_markup=_dur_common_kb(st["duration_common_min"])
            )
        else:
            st["dur_mode"] = "per_day"
            per_day_time: Dict[str, str] = st.get("schedule_map_time") or {}
            pending = [d for d in ORDERED_DAYS if d in per_day_time]
            st["pending_days_dur"] = pending
            st["step"] = "ask_duration_for_day_inline"
            first = pending.pop(0)
            st["temp_day_en"] = first
            ru = RU_FULL_BY_EN.get(first, first)
            await cq.message.reply_text(f"⏲️ Минуты для {ru}:", reply_markup=_dur_perday_kb(first, 60))
        await _safe_cq_answer(cq)
        return

    # ====== Общая длительность ======
    if data.startswith("dur_common_set:"):
        v = int(data.split(":", 1)[1])
        if v != int(st.get("duration_common_min") or 60):
            st["duration_common_min"] = v
            await _safe_edit_reply_markup(cq.message, _dur_common_kb(v))
        await _safe_cq_answer(cq, f"{v} мин")
        return

    if data.startswith("dur_common_adj:"):
        delta = int(data.split(":", 1)[1])
        cur = int(st.get("duration_common_min") or 60)
        v = max(5, min(240, cur + delta))
        if v != cur:
            st["duration_common_min"] = v
            await _safe_edit_reply_markup(cq.message, _dur_common_kb(v))
        await _safe_cq_answer(cq, f"{v} мин")
        return

    if data == "dur_common_custom":
        st["step"] = "ask_duration_common"
        await cq.message.reply_text("Введи общее количество минут (5–240):")
        await _safe_cq_answer(cq)
        return

    if data == "dur_common_done":
        v = int(st.get("duration_common_min") or 60)
        _set_window_seconds_cached(update.effective_user.id, int(v) * 60)
        # ⬇️ СРАЗУ запускаем ИИ-рекомендацию залога
        await _auto_deposit_and_finish(cq.message, update, context, st)
        await _safe_cq_answer(cq, "Готово")
        return

    # ====== Длительность по дням ======
    if data.startswith("dur_pd_set:"):
        _, day_en, v = data.split(":")
        st.setdefault("schedule_map_duration", {})[day_en] = int(v)
        pend = st.get("pending_days_dur", [])
        if pend:
            next_day = pend.pop(0)
            st["temp_day_en"] = next_day
            ru = RU_FULL_BY_EN.get(next_day, next_day)
            await cq.message.reply_text(f"⏲️ Минуты для {ru}:", reply_markup=_dur_perday_kb(next_day, 60))
            await _safe_cq_answer(cq, f"{EN2RU_SHORT.get(day_en, day_en)} — {v} мин")
            return

        # это был последний день → запускаем ИИ-рекомендацию залога
        try:
            any_dur = next(iter(st.get("schedule_map_duration", {}).values()))
        except Exception:
            any_dur = 60
        _set_window_seconds_cached(update.effective_user.id, int(any_dur) * 60)
        await _auto_deposit_and_finish(cq.message, update, context, st)
        await _safe_cq_answer(cq, f"{EN2RU_SHORT.get(day_en, day_en)} — {v} мин")
        return

    if data.startswith("dur_pd_custom:"):
        _, day_en = data.split(":")
        st["temp_day_en"] = day_en
        st["step"] = "ask_duration_for_day_custom"
        ru = RU_FULL_BY_EN.get(day_en, day_en)
        await cq.message.reply_text(f"Введи минуты (5–240) для {ru}:")
        await _safe_cq_answer(cq)
        return

    # ====== Залог: выбор/кастом после ИИ-рекомендации ======
    if data == "dep_ok":
        st["step"] = "ask_deposit_days"
        await cq.message.reply_text("📅 На сколько дней заморозить залог? Введи число (1–90), например 7, 14, 21.")
        await _safe_cq_answer(cq, "Ок")
        return

    if data == "dep_custom":
        st["step"] = "ask_deposit_custom"
        await cq.message.reply_text("✏️ Введи свою сумму залога (₽). Допускается только число, диапазон 500–100000.")
        await _safe_cq_answer(cq, "Введи свою сумму")
        return

    await _safe_cq_answer(cq)


# ---------------- Хендлеры верхнего уровня ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_start(update, context)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        await (update.effective_message or message).reply_text(
            "Не понял. Нажми кнопку ниже.",
            reply_markup=_current_keyboard(context, update.effective_user.id)
        )
        return

    msg = message.text.strip()
    low = msg.lower()
    user = update.effective_user

    # Запуск регистрации из меню
    if low in ("📝 регистрация", "регистрация", "/register"):
        if await _already_registered(user.id):
            await message.reply_text(
                "Ты уже зарегистрирован ✅\n",
                reply_markup=_make_keyboard(False, user.id)
            )
            return
        await register_start(update, context)
        return

    if _reg_active(context):
        await register_text(update, context)
        return

    # Админ-кнопки (здесь только ранний выход — сами команды идут ниже в файле)
    if _is_admin(user.id):
        if low in ("🟢 старт тренировки (админ)", "старт тренировки", "🟢 старт тренировки", "/start_workout"):
            _set_session_active(context, user.id, True)
            _ws_reset(context, user.id)
            _ws_get(context, user.id)
            await message.reply_text(
                "🚀 Режим тренировки включён (админ). Жми «▶️ Начать тренировку». Будет 3 снимка с паузами отдыха.",
                reply_markup = await _build_workout_keyboard(context, user.id)
            )
            return
        if low in ("🔴 стоп тренировки (админ)", "стоп тренировки", "🔴 стоп тренировки", "/end_workout"):
            _set_session_active(context, user.id, False)
            _ws_reset(context, user.id)
            await message.reply_text("🛑 Режим тренировки выключен (админ).",
                                     reply_markup=_make_keyboard(False, user.id))
            return
        if low in ("🧹 очистить мои данные", "/clear_me"):
            await clear_my_data(update, context)
            return

    # Мини-мастер изменения залога через текст (если активирован ранее в колбэках)
    if context.user_data.get("dep_edit"):
        st = context.user_data["dep_edit"]

        if st.get("await") == "amount":
            m = re.search(r"\d{1,6}", msg.replace(" ", ""))
            if not m:
                await message.reply_text("Нужно целое число в ₽. Пример: 3000")
                return
            amount = _clamp_deposit(int(m.group(0)))
            st["amount"] = amount
            st["await"] = "days"
            await message.reply_text("📅 На сколько дней заморозить залог? Введи число 1–90 (например 7, 14, 21).")
            return

        if st.get("await") == "days":
            m = re.search(r"\d{1,3}", msg)
            if not m:
                await message.reply_text("Введи просто число дней (1–90).")
                return
            days = int(m.group(0))
            if not (1 <= days <= 90):
                await message.reply_text("Число вне диапазона. Разрешено 1–90 дней.")
                return

            try:
                async with Database.acquire() as conn:
                    row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id = $1", user.id)
                tf = _load_training_form(row.get("training_form") if row else None) or {}
                tf["deposit"] = int(st["amount"])
                tf["deposit_days"] = days
                tf["deposit_done_dates"] = []
                tf["deposit_started_at"] = datetime.now(_tz_for(user.id)).isoformat()
                tf["deposit_forfeit"] = False
                tf["deposit_forfeit_reason"] = ""
                tf["deposit_forfeit_at"] = None
                tf["deposit_left"] = int(st["amount"])

                async with Database.acquire() as conn:
                    await conn.execute(
                        "UPDATE users SET training_form = $2 WHERE user_id = $1",
                        user.id, json.dumps(tf, ensure_ascii=False)
                    )

                await message.reply_text(f"✅ Залог обновлён: {tf['deposit']} ₽ на {days} дн.")
            except Exception as e:
                logger.exception("dep_edit save failed: %s", e)
                await message.reply_text("⚠️ Не удалось сохранить новый залог. Попробуй ещё раз.")

            context.user_data.pop("dep_edit", None)
            return

    # Мастер напоминаний (общий случай)
    if context.user_data.get("awaiting_reminder_days"):
        days = _parse_days(msg)
        if not days:
            async with Database.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE users
                       SET reminder_enabled = FALSE,
                           reminder_days = $2,
                           reminder_time = NULL,
                           workout_duration = NULL
                     WHERE user_id = $1
                    """,
                    user.id, days
                )
            await _reschedule_from_db(update, context, user.id)
            _set_session_active(context, user.id, False)
            context.user_data.clear()
            await message.reply_text(
                "🔕 Напоминания отключены. Когда начнёшь — просто тренируйся.",
                reply_markup=_make_keyboard(False, user.id)
            )
            return

        context.user_data["reminder_days"] = days
        context.user_data.pop("awaiting_reminder_days", None)
        context.user_data["awaiting_reminder_time"] = True
        await message.reply_text(
            "⏰ Во сколько напоминать? Например 07:00, 19:30 или нажми кнопку.",
            reply_markup=time_keyboard()
        )
        return

    if context.user_data.get("awaiting_reminder_time"):
        if low == "другое время":
            await message.reply_text(
                "Введи время в формате ЧЧ:ММ, например 19:30.",
                reply_markup=time_keyboard()
            )
            return

        t = _parse_time_hhmm(msg)
        if not t:
            await message.reply_text(
                "Не понял время. Введи в формате ЧЧ:ММ (например, 08:00).",
                reply_markup=time_keyboard()
            )
            return
        context.user_data["reminder_time"] = t
        context.user_data.pop("awaiting_reminder_time", None)

        context.user_data["awaiting_reminder_duration"] = True
        await message.reply_text(
            "⏱️ Введи длительность тренировки в минутах (5–240) или выбери кнопку.",
            reply_markup=duration_keyboard()
        )
        return

    if context.user_data.get("awaiting_reminder_duration"):
        dur = _parse_duration_minutes(msg)
        if dur is None:
            await message.reply_text(
                "Введи число минут (от 5 до 240), например: 30, 60, 95.",
                reply_markup=duration_keyboard()
            )
            return

        context.user_data["workout_duration"] = dur
        context.user_data.pop("awaiting_reminder_duration", None)

        days = context.user_data.get("reminder_days", [])
        t = context.user_data.get("reminder_time")

        async with Database.acquire() as conn:
            await conn.execute(
                """
                UPDATE users
                   SET reminder_enabled = TRUE,
                       reminder_days = $2,
                       reminder_time = $3,
                       workout_duration = $4
                 WHERE user_id = $1
                """,
                user.id, days, t, dur
            )

        per_day_time = {d: t.strftime("%H:%M") for d in days if isinstance(t, time)}
        per_day_duration = {d: dur for d in days}
        _schedule_reminders_per_day(context, user.id, per_day_time, per_day_duration, default_duration_min=dur)

        _set_window_seconds_cached(user.id, int(dur) * 60)
        context.user_data.clear()

        pretty = " ".join(RU_BY_EN.get(d, d) for d in days)
        await message.reply_text(
            f"✅ Напоминания включены.\n"
            f"Дни: {pretty}\n"
            f"Время: {t.strftime('%H:%M')}\n"
            f"Длительность: {dur} мин.",
            reply_markup=_make_keyboard(False, user.id)
        )
        return

    # Изменение залога — шаг 1/2 (через профиль)
    if context.user_data.get("awaiting_dep_amount"):
        m = re.search(r"\d{1,6}", msg.replace(" ", ""))
        if not m:
            await message.reply_text("Нужно число ₽ (500–100000). Пример: 6000")
            return
        amount = _clamp_deposit(int(m.group(0)))
        context.user_data["new_deposit_amount"] = amount
        context.user_data.pop("awaiting_dep_amount", None)
        context.user_data["awaiting_dep_days"] = True
        await message.reply_text("📅 На сколько дней заморозить? Введи число 1–90. Пример: 14")
        return

    # Изменение залога — шаг 2/2
    if context.user_data.get("awaiting_dep_days"):
        m = re.search(r"\d{1,3}", msg)
        if not m:
            await message.reply_text("Введи число дней 1–90. Пример: 21")
            return
        days = max(1, min(90, int(m.group(0))))
        amount = int(context.user_data.get("new_deposit_amount"))
        context.user_data.pop("awaiting_dep_days", None)
        context.user_data.pop("new_deposit_amount", None)

        try:
            await _update_deposit_in_db(update.effective_user.id, deposit=amount, deposit_days=days,
                                        restart_window=True)
            await message.reply_text(
                f"✅ Обновлено: залог {amount} ₽ на {days} дн. Новое окно запущено с сегодняшнего дня.",
                reply_markup=_current_keyboard(context, update.effective_user.id)
            )
        except Exception as e:
            logger.exception("update dep failed: %s", e)
            await message.reply_text("⚠️ Не получилось сохранить изменения. Попробуй ещё раз.")
        return

    if low in ("профиль", "📊 профиль"):
        await profile(update, context)
        return

    await message.reply_text("Не понял. Нажми кнопку ниже.",
                             reply_markup=_current_keyboard(context, user.id))

async def reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    context.user_data.clear()
    context.user_data["awaiting_reminder_days"] = True
    await message.reply_text(
        "🗓️ Обновим расписание. В какие дни тренируешься?\n"
        "• «пн ср пт»  • «вт чт сб»  • «пн-пт»  • «каждый день»  • «сб вс»  • «без расписания»",
        reply_markup=days_keyboard(),
    )

# ---------------- Приём данных из WebApp (фиксы дубликатов) ----------------
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.web_app_data:
        return
    try:
        raw = update.message.web_app_data.data
        payload = json.loads(raw)
    except Exception:
        logger.exception("[webapp] failed to parse web_app_data JSON")
        return

    ptype = str(payload.get("type") or "")

    # автоопределение TZ
    if ptype == "tz":
        user = update.effective_user
        tz_name = (payload.get("tz") or "").strip()
        if not tz_name:
            await update.message.reply_text("⚠️ Не удалось определить часовой пояс.")
            return
        try:
            ZoneInfo(tz_name)
        except Exception:
            await update.message.reply_text(f"⚠️ Неизвестный часовой пояс: {tz_name}")
            return

        async with Database.acquire() as conn:
            await conn.execute(
                "UPDATE users SET timezone = $2 WHERE user_id = $1",
                user.id, tz_name
            )
        _set_tz_for(user.id, tz_name)

        await _reschedule_from_db(update, context, user.id)
        await update.message.reply_text(f"🕒 Часовой пояс обновлён: {tz_name}")
        return

    # одиночное фото тренировки
    if ptype in ("single_photo_uploaded", "set_photo_uploaded"):
        user = update.effective_user
        token = payload.get("token") or payload.get("t") or payload.get("id")
        if not token:
            logger.warning("[webapp] user=%s %s without token", user.id, ptype)
            return

        try:
            async with aiohttp.ClientSession() as sess:
                pull_url = settings.make_pull_url(token)
                async with sess.get(pull_url, timeout=30) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    photo_bytes = await r.read()
        except Exception as e:
            logger.exception("[webapp] user=%s pull fail: %s", user.id, e)
            await update.message.reply_text("⚠️ Не удалось получить фото. Попробуй ещё раз.")
            return

        ok = await _save_training_photo(user.id, photo_bytes, context.bot, notify=False)
        ws = _ws_get(context, user.id)
        ws["results"].append(ok)

        jq = getattr(context.application, "job_queue", None)
        if jq:
            for job in jq.jobs():
                if (job.name or "") == f"{user.id}:nostart":
                    job.schedule_removal()

        if len(ws["results"]) >= ws["expected"]:
            await _finalize_workout(context, user.id, ws["results"])
            _ws_reset(context, user.id)
            _set_session_active(context, user.id, False)
        else:
            await update.message.reply_text(f"Фото получено ({len(ws['results'])}/3). Продолжаем…")
        return

    # сет фото тренировки
    if ptype == "workout_set":
        user = update.effective_user
        tokens = payload.get("tokens") or payload.get("photos") or []
        if not tokens:
            return

        photos_bytes: List[bytes] = []
        async with aiohttp.ClientSession() as sess:
            for idx, tok in enumerate(tokens, start=1):
                try:
                    pull_url = settings.make_pull_url(tok)
                    async with sess.get(pull_url, timeout=30) as r:
                        if r.status != 200:
                            raise RuntimeError(f"HTTP {r.status}")
                        data = await r.read()
                        photos_bytes.append(data)
                except Exception as e:
                    logger.exception("[webapp] user=%s fail pull %d/%d: %s",
                                     user.id, idx, len(tokens), e)
                    await update.message.reply_text("⚠️ Не удалось получить фото. Попробуй ещё раз.")
                    return

        ws = _ws_get(context, user.id)
        results = []
        for pb in photos_bytes:
            ok = await _save_training_photo(user.id, pb, context.bot, notify=False)
            results.append(ok)
            ws["results"].append(ok)

        await _finalize_workout(context, user.id, ws["results"])
        _ws_reset(context, user.id)
        _set_session_active(context, user.id, False)
        return

    logger.info("[webapp] skip payload type=%r", ptype)

# ---------------- Фото-проверка тренировки ----------------
async def _save_training_photo(user_id: int, photo_bytes: bytes, bot, notify: bool = False) -> bool:
    from tempfile import NamedTemporaryFile
    from pathlib import Path

    with NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(photo_bytes)
        tmp.flush()
        tmp_path = tmp.name

    try:
        check_text = (
            "Оцени фото как доказательство тренировки ДОМА.\n"
            "Критерии:\n"
            "1) На фото человек ВЫПОЛНЯЕТ упражнение (а не позирует/селфи/показывает инвентарь).\n"
            "2) Фото актуально, не скриншот, без монтажей.\n"
            "3) ЛОКАЦИЯ: жилое помещение (квартира/комната/дом) или домашний инвентарь; "
            "НЕ допускается коммерческий зал/публичный фитнес-центр.\n"
            "Верни строго JSON: {\"success\": bool, \"is_home\": bool, \"reason\": string}."
        )
        gpt = await verify_task_with_gpt(check_text, tmp_path)
        verified = bool(gpt.get("success"))
        is_home = bool(gpt.get("is_home"))
        reason = gpt.get("reason", "")

        if verified and not is_home:
            verified = False
            reason = reason or "Обстановка не похожа на домашнюю"

        async with Database.acquire() as conn:
            await conn.execute(
                "INSERT INTO sets (user_id, photo, verified, gpt_reason) VALUES ($1, $2, $3, $4)",
                user_id, photo_bytes, verified, reason
            )

        if notify:
            if verified:
                await bot.send_message(chat_id=user_id, text="✅ Фото засчитано (дом).")
            else:
                await bot.send_message(chat_id=user_id, text="❌ Фото не засчитано: " + (reason or "не прошла проверка"))
        return verified
    except Exception as e:
        logger.exception("Photo verify/save failed: %s", e)
        try:
            if notify:
                await bot.send_message(chat_id=user_id, text="⚠️ Не удалось проверить фото. Попробуй ещё раз.")
        except Exception:
            pass
        return False
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

async def _finalize_workout(context: ContextTypes.DEFAULT_TYPE, user_id: int, results: List[bool]) -> None:
    ws = _ws_get(context, user_id)
    expected = int(ws.get("expected", 3))
    received = len(results)
    verified = sum(1 for x in results if x)

    threshold = expected - 1 if expected >= 3 else expected

    def _cancel_timers():
        jq = getattr(context.application, "job_queue", None)
        if jq:
            for job in jq.jobs():
                if (job.name or "") in (f"{user_id}:nostart", f"{user_id}:noresult"):
                    job.schedule_removal()

    if verified >= threshold:
        tail = ""
        if received < expected:
            tail = " (прислал не все фото, но зачёт есть)"
        elif verified < expected:
            tail = " (одно фото не прошло, но зачёт есть)"

        await context.bot.send_message(
            chat_id=user_id,
            text=f"🏁 Тренировка: ✅ засчитана — подтверждено {verified}/{expected}{tail}."
        )
        _cancel_timers()

        try:
            async with Database.acquire() as conn:
                row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id=$1", user_id)
            tf = _load_training_form(row.get("training_form") if row else None) or {}

            deposit_days = int(tf.get("deposit_days") or 0)
            if deposit_days > 0:
                tz = _tz_for(user_id)
                today_iso = datetime.now(tz).date().isoformat()
                tf.setdefault("deposit_started_at", today_iso)

                done_dates = list(tf.get("deposit_done_dates") or [])
                if today_iso not in done_dates:
                    done_dates.append(today_iso)
                    tf["deposit_done_dates"] = done_dates

                async with Database.acquire() as conn:
                    await conn.execute(
                        "UPDATE users SET training_form=$2 WHERE user_id=$1",
                        user_id, json.dumps(tf, ensure_ascii=False)
                    )

                if len(done_dates) >= deposit_days:
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text="🎉 Прогресс по заморозке выполнен полностью!\n\nВыбери, что делаем дальше:",
                            reply_markup=_deposit_complete_kb()
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.exception("update deposit progress failed: %s", e)

        return

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "🏁 Тренировка: ❌ не засчитана.\n"
            f"Получено: {received}/{expected}, подтверждено: {verified}/{expected}.\n"
            f"Для зачёта нужно ≥ {threshold}."
        )
    )

    await _send_last_photos_with_reasons(context, user_id, limit=expected)

    try:
        admin_username = getattr(settings, "ADMIN_USERNAME", None)
        if admin_username:
            await context.bot.send_message(chat_id=user_id, text=f"💬 Обжалование: напиши @{admin_username}.")
        else:
            await context.bot.send_message(chat_id=user_id, text="💬 Обжалование: напиши администратору.")
    except Exception:
        pass

    await _forfeit_deposit(context, user_id, f"недостаточно подтверждённых фото ({verified}/{expected})")
    _cancel_timers()

async def _send_last_photos_with_reasons(context: ContextTypes.DEFAULT_TYPE, user_id: int, limit: int = 3) -> None:
    try:
        async with Database.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT photo, verified, COALESCE(gpt_reason,'') AS gpt_reason, 
                       COALESCE(created_at, NOW()) AS created_at
                  FROM sets
                 WHERE user_id = $1
                 ORDER BY created_at DESC
                 LIMIT $2
                """,
                user_id, limit
            )
    except Exception as e:
        logger.exception("fetch last photos failed: %s", e)
        rows = []

    if not rows:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Фото этой тренировки не найдены в базе. Если это ошибка — напиши админу."
            )
        except Exception:
            pass
        return

    def _cap(verified: bool, reason: str, idx: int) -> str:
        status = "❌ Не засчитано" if not verified else "✅ Засчитано"
        reason = (reason or "").strip()
        if reason:
            return f"{idx}. {status}\nПричина: {reason}"
        return f"{idx}. {status}"

    media: List[InputMediaPhoto] = []
    for i, r in enumerate(rows[::-1], start=1):
        b = bytes(r.get("photo") or b"")
        cap = _cap(bool(r.get("verified")), str(r.get("gpt_reason") or ""), i)
        bio = BytesIO(b)
        bio.name = f"workout_{i}.jpg"
        try:
            media.append(InputMediaPhoto(media=InputFile(bio, filename=bio.name),
                                         caption=cap[:1024]))
        except Exception:
            media = []
            break

    if media:
        try:
            await context.bot.send_media_group(chat_id=user_id, media=media)
            return
        except Exception as e:
            logger.exception("send_media_group failed, will fallback to singles: %s", e)

    for i, r in enumerate(rows[::-1], start=1):
        b = bytes(r.get("photo") or b"")
        cap = _cap(bool(r.get("verified")), str(r.get("gpt_reason") or ""), i)
        bio = BytesIO(b)
        bio.name = f"workout_{i}.jpg"
        try:
            await context.bot.send_photo(chat_id=user_id, photo=bio, caption=cap[:1024])
        except Exception as e:
            logger.exception("send_photo failed: %s", e)
            try:
                await context.bot.send_message(chat_id=user_id, text=cap)
            except Exception:
                pass

# ---------------- Профиль ----------------
async def _update_deposit_in_db(user_id: int, deposit: int, deposit_days: int, restart_window: bool = False) -> None:
    async with Database.acquire() as conn:
        row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id=$1", user_id)
    tf = _load_training_form(row.get("training_form") if row else None) or {}

    tf["deposit"] = int(deposit)
    tf["deposit_days"] = int(deposit_days)

    if restart_window:
        tf["deposit_done_dates"] = []
        tf["deposit_started_at"] = datetime.now(_tz_for(user_id)).isoformat()
        tf["deposit_forfeit"] = False
        tf["deposit_forfeit_reason"] = ""
        tf["deposit_forfeit_at"] = None
        tf["deposit_left"] = int(deposit)

    async with Database.acquire() as conn:
        await conn.execute(
            "UPDATE users SET training_form=$2 WHERE user_id=$1",
            user_id, json.dumps(tf, ensure_ascii=False)
        )

# ---------- PROFILE (drop-in) ----------
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tz = _tz_for(user.id)
    now = datetime.now(tz)

    # читаем всё нужное из users
    async with Database.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT username, first_name, last_name,
                   reminder_enabled, reminder_days, reminder_time,
                   workout_duration, rest_seconds, training_form, timezone
              FROM users
             WHERE user_id = $1
        """, user.id)

    if not row:
        await update.effective_message.reply_text(
            "Профиль не найден. Пройди быструю регистрацию.",
            reply_markup=_make_keyboard(False, user.id)
        )
        return

    # TZ
    tz_name = row.get("timezone") or getattr(settings, "TIMEZONE", "Europe/Moscow")
    _set_tz_for(user.id, tz_name)
    tz = _tz_for(user.id)

    # Настройки
    reminder_enabled = bool(row.get("reminder_enabled"))
    rest_seconds = int(row.get("rest_seconds") or 60)

    # training_form
    # training_form (старое/тонкое расписание)
    tf = _load_training_form(row.get("training_form"))
    per_day_time: Dict[str, str] = (tf.get("per_day_time") or {})
    per_day_duration: Optional[Dict[str, int]] = (tf.get("per_day_duration") or None)

    # legacy (новые «каждый день 08:00 × 30» из мастера напоминаний)
    legacy_days = list(row.get("reminder_days") or [])
    t: Optional[time] = row.get("reminder_time")
    dur = int(row.get("workout_duration") or 0)

    legacy_time = {}
    legacy_dur = None
    if legacy_days and isinstance(t, time) and dur:
        legacy_time = {d: t.strftime("%H:%M") for d in legacy_days}
        legacy_dur = {d: dur for d in legacy_days}

    # ✅ Приоритет: если legacy заполнен и по множеству дней отличается от TF — показываем legacy
    if legacy_time:
        tf_days = set((per_day_time or {}).keys())
        legacy_days_set = set(legacy_time.keys())
        if not per_day_time or (legacy_days_set != tf_days):
            per_day_time = legacy_time
            per_day_duration = legacy_dur

    # Фолбэк на старые поля, если per_day_time ещё пуст
    if not per_day_time:
        legacy_days = list(row.get("reminder_days") or [])
        t: Optional[time] = row.get("reminder_time")
        dur = int(row.get("workout_duration") or 60)
        if legacy_days and isinstance(t, time):
            per_day_time = {d: t.strftime("%H:%M") for d in legacy_days}
            per_day_duration = {d: dur for d in legacy_days}
        else:
            per_day_time = {}
            per_day_duration = None

    # Строки «Дни/время/длительность»
    sched_lines = _human_schedule_lines(per_day_time, per_day_duration)

    # Анкета
    answers = tf.get("answers") or {}
    a1 = str(answers.get("q1", "")).strip()
    a2 = str(answers.get("q2", "")).strip()
    a3 = str(answers.get("q3", "")).strip()

    # Залог
    dep_line = _format_deposit_status(tf, tz)
    deposit_days = int(tf.get("deposit_days") or 0)
    done_dates = list(tf.get("deposit_done_dates") or [])
    done_cnt = len(done_dates)
    progress_bar = _progress_bar(done_cnt, deposit_days, width=20)
    started_at = (tf.get("deposit_started_at") or "").strip()

    # План-текст/видео: короткий индикатор
    has_plan_text = bool((tf.get("workout_text") or "").strip())
    has_plan_video = bool((tf.get("workout_video_url") or "").strip())
    plan_text_flag = "да" if has_plan_text else "—"
    plan_video_flag = "да" if has_plan_video else "—"

    # Режим тренировки (из вашего флага session_active)
    session_on = _is_session_active(context, user.id)
    session_line = "включен" if session_on else "выключен"

    # Заголовок профиля
    who = f"@{row.get('username')}" if row.get('username') else (user.first_name or str(user.id))
    dt_str = now.strftime("%Y-%m-%d (%A) %H:%M")
    header = f"👤 Профиль {who}\n{dt_str} ({tz.key})"

    # Напоминания
    bell = "включены" if reminder_enabled and per_day_time else "выключены"

    parts = [
        header,
        "",
        f"🔔 Напоминания: {bell}",
        "",
        "Дни/время/длительность",
    ]

    if sched_lines:
        parts += [f"• {line}" for line in sched_lines]
    else:
        parts.append("• без расписания")

    parts += [
        "",
        f"Отдых: {rest_seconds} сек.",
        "",
        "📝 Анкета",
        f"• 1) Почему начинаешь сейчас? Что важно?\n{(a1 or '—')}",
        f"• 2) Цель на 4 недели (измеримая)?\n{(a2 or '—')}",
        f"• 3) Что чаще всего срывает и как обойти?\n{(a3 or '—')}",
        f"{dep_line}",
        f"• План (текст): {plan_text_flag}",
        f"• План (видео): {plan_video_flag}",
        "",
        "💰 Прогресс по заморозке",
        f"{done_cnt}/{deposit_days or 0} ({(0 if deposit_days == 0 else int(done_cnt*100/max(1,deposit_days)))}%)",
        progress_bar,
    ]
    if started_at:
        parts.append(f"Старт окна: {started_at}")

    parts += [
        "",
        f"Режим тренировки: {session_line}",
    ]

    text = "\n".join(parts)

    await update.effective_message.reply_text(
        text,
        reply_markup=_current_keyboard(context, user.id)
    )
# ---------- end PROFILE ----------

# ---------------- Админ-команды ----------------
async def delete_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.effective_message.reply_text("🚫 Доступ запрещён.", reply_markup=_current_keyboard(context, user.id))
        return
    try:
        await Database.drop()
        await Database.init()
        _clear_user_jobs(context, user.id)
        context.application.bot_data["session_active"] = {}
        await update.effective_message.reply_text("🗑️ База данных удалена и пересоздана.",
                                                  reply_markup=_make_keyboard(False, user.id))
    except Exception as e:
        logger.exception("/delete_db failed: %s", e)
        await update.effective_message.reply_text("⚠️ Ошибка при удалении БД.", reply_markup=_make_keyboard(False, user.id))

async def clear_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.effective_message.reply_text("🚫 Доступ запрещён.", reply_markup=_current_keyboard(context, user.id))
        return
    try:
        async with Database.acquire() as conn:
            try:
                await conn.execute("TRUNCATE TABLE tasks RESTART IDENTITY CASCADE")
            except Exception:
                pass
            try:
                await conn.execute("TRUNCATE TABLE sets RESTART IDENTITY CASCADE")
            except Exception:
                pass
            try:
                await conn.execute(
                    """
                    UPDATE users
                       SET reminder_enabled = FALSE,
                           reminder_days = ARRAY[]::text[],
                           reminder_time = NULL,
                           workout_duration = NULL,
                           training_form = NULL
                    """
                )
            except Exception:
                pass

        _clear_user_jobs(context, user.id)
        context.application.bot_data["session_active"] = {}

        await update.effective_message.reply_text("🧹 Данные очищены. Напоминания выключены у всех.",
                                                  reply_markup=_make_keyboard(False, user.id))
    except Exception as e:
        logger.exception("/clear_db failed: %s", e)
        await update.effective_message.reply_text("⚠️ Ошибка при очистке данных.", reply_markup=_make_keyboard(False, user.id))

# Дополнительно: команды для ручного старта/стопа
async def start_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.effective_message.reply_text("🚫 Доступ запрещён.", reply_markup=_current_keyboard(context, user.id))
        return
    _set_session_active(context, user.id, True)
    _ws_reset(context, user.id)
    _ws_get(context, user.id)
    await update.effective_message.reply_text(
        "🚀 Режим тренировки включён (админ). Жми «▶️ Начать тренировку». Будет 3 снимка с паузами отдыха.",
        reply_markup = await _build_workout_keyboard(context, user.id)
    )

async def end_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.effective_message.reply_text("🚫 Доступ запрещён.", reply_markup=_current_keyboard(context, user.id))
        return
    _set_session_active(context, user.id, False)
    _ws_reset(context, user.id)
    await update.effective_message.reply_text("🛑 Режим тренировки выключен (админ).",
                                              reply_markup=_make_keyboard(False, user.id))
async def deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data or ""
    user = update.effective_user
    await _safe_cq_answer(cq)

    # Достаём текущую анкету (training_form)
    tf = {}
    try:
        async with Database.acquire() as conn:
            row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id = $1", user.id)
        tf = _load_training_form(row.get("training_form") if row else None) or {}
    except Exception as e:
        logger.exception("deposit_callback: read training_form failed: %s", e)

    # Повторить заморозку: обнуляем прогресс и ставим новую дату старта
    if data == "depwin_repeat":
        tf.setdefault("deposit", tf.get("deposit") or 0)
        tf.setdefault("deposit_days", tf.get("deposit_days") or 7)
        tf["deposit_done_dates"] = []
        tf["deposit_started_at"] = datetime.now(_tz_for(user.id)).isoformat()

        try:
            async with Database.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET training_form = $2 WHERE user_id = $1",
                    user.id, json.dumps(tf, ensure_ascii=False)
                )
            await cq.message.reply_text("✅ Новая заморозка запущена. Удачи!")
        except Exception as e:
            logger.exception("depwin_repeat save failed: %s", e)
            await cq.message.reply_text("⚠️ Не удалось перезапустить заморозку. Попробуй ещё раз.")
        return

    # Меняем только залог: запускаем мини-мастер через текст
    if data == "depwin_change_amount":
        context.user_data["dep_edit"] = {"await": "amount"}  # ждём сумму
        await cq.message.reply_text("✏️ Введи новую сумму залога (₽). Диапазон 500–100000.")
        return

    # Меняем расписание: запускаем мастер напоминаний
    if data == "depwin_change_sched":
        await reminders(update, context)
        return

    # Рестарт после списания
    if data == "depforf_restart":
        amount = int(tf.get("deposit") or 0)
        days = int(tf.get("deposit_days") or 0)

        if amount <= 0:
            context.user_data["dep_edit"] = {"await": "amount"}
            await cq.message.reply_text("✏️ Введи новую сумму залога (₽). Диапазон 500–100000.")
            return
        if not (1 <= days <= 90):
            context.user_data["dep_edit"] = {"await": "days", "amount": amount}
            await cq.message.reply_text("📅 На сколько дней заморозить залог? Введи число 1–90 (например 7, 14, 21).")
            return

        try:
            tf["deposit_forfeit"] = False
            tf["deposit_forfeit_reason"] = ""
            tf["deposit_forfeit_at"] = None
            tf["deposit_left"] = amount
            tf["deposit_done_dates"] = []
            tf["deposit_started_at"] = datetime.now(_tz_for(user.id)).isoformat()

            async with Database.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET training_form = $2 WHERE user_id = $1",
                    user.id, json.dumps(tf, ensure_ascii=False)
                )
            await cq.message.reply_text("✅ Новая заморозка запущена. Удачи!")
        except Exception as e:
            logger.exception("depforf_restart save failed: %s", e)
            await cq.message.reply_text("⚠️ Не удалось перезапустить заморозку. Попробуй ещё раз.")
        return

    # Закрыть
    if data == "depwin_later":
        await _safe_cq_answer(cq, "Ок")
        return
