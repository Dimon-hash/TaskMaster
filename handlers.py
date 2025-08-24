import logging
import re
import json
import html  # ✅ для безопасного HTML-вывода
from datetime import datetime, timedelta, time, date, timezone as dt_timezone
from typing import List, Optional, Dict, Tuple

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
)
from telegram.ext import ContextTypes
from telegram.error import BadRequest  # ✅ для безопасного редактирования клавиатуры

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
        if user_id == getattr(settings, "ADMIN_ID", 0):
            return True
        admin_ids = set(getattr(settings, "ADMIN_IDS", []) or [])
        return user_id in admin_ids
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
async def _safe_edit_reply_markup(message: Message, reply_markup: InlineKeyboardMarkup) -> None:
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
def _make_keyboard(is_workout: bool, user_id: int) -> ReplyKeyboardMarkup:
    rows = []
    if is_workout:
        rest_sec = _get_rest_seconds_cached(user_id)
        window_sec = _get_window_seconds_cached(user_id)
        rows.append([
            KeyboardButton(
                "▶️ Начать тренировку",
                web_app=WebAppInfo(
                    url=str(settings.WEBAPP_URL)
                    + "?mode=workout"
                    + "&shots=3"
                    + f"&rest={rest_sec}"
                    + f"&window={window_sec}"
                    + "&verify=home"
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
def _deposit_complete_kb(chosen: str | None = None, locked: bool = False) -> InlineKeyboardMarkup:
    """
    Если locked=True — все кнопки становятся «заблокированными» (callback_data='dep_locked'),
    а выбранная помечается галочкой.
    """
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
    """
    Кнопки для экрана «залог списан».
    chosen — какой callback был выбран (подсветим '✅ ').
    locked=True — превращаем все кнопки в неактивные (callback_data='dep_locked').
    """
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
    """
    Возвращает человекочитаемую строку про залог:
    - 'Залог: 6000 ₽ (на кону)'
    - 'Залог: списан 6000 ₽ — <дата> (причина: ...)'
    - 'Залог: —' если суммы нет
    """
    dep = tf.get("deposit")
    if dep is None:
        return "• Залог: —"

    dep = int(dep or 0)
    forfeited = bool(tf.get("deposit_forfeit"))
    left = int(tf.get("deposit_left") or 0)
    reason = (tf.get("deposit_forfeit_reason") or "").strip()
    forfeited_at = tf.get("deposit_forfeit_at")

    if forfeited:
        # красивая дата списания в TZ пользователя
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

    # не списан: показываем сумму и оставшуюся (если поле есть)
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
            # сносим только свои записи
            try:
                await conn.execute("DELETE FROM tasks WHERE user_id=$1", user.id)
            except Exception:
                pass
            try:
                await conn.execute("DELETE FROM sets  WHERE user_id=$1", user.id)
            except Exception:
                pass
            # мягкий сброс профиля, TZ не трогаем
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

        # чистим планировщик и кэши только для себя
        _clear_user_jobs(context, user.id)
        _set_session_active(context, user.id, False)
        REST_CACHE.pop(user.id, None)
        WORKOUT_WINDOW_CACHE.pop(user.id, None)
        # TZ оставляем; если нужно — раскомментируй:
        # TZ_CACHE.pop(user.id, None)

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
            ws = _ws_get(ctx, uid)

            # Узнаём сумму залога
            dep_amt = 0
            try:
                async with Database.acquire() as conn:
                    row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id=$1", uid)
                tf = _load_training_form(row.get("training_form") if row else None) or {}
                if not tf.get("deposit_forfeit"):
                    # показываем остаток, если поле есть, иначе полную сумму
                    left = int(tf.get("deposit_left") or 0)
                    dep_amt = left if left > 0 else int(tf.get("deposit") or 0)
            except Exception:
                dep_amt = 0

            intro_line = "🏁 Старт окна! Жми «▶️ Начать тренировку»."
            money_line = f"\n💸 На кону: {dep_amt} ₽. Начни в течение 5 минут, иначе деньги спишутся." \
                if dep_amt > 0 else ""
            try:
                await ctx.bot.send_message(
                    chat_id=uid,
                    text=f"{intro_line}{money_line}\nБудет 3 снимка с паузами отдыха.",
                    reply_markup=_make_keyboard(True, uid)
                )
            except Exception:
                logger.exception("Failed to send START reminder")

            # Ставим одноразовую проверку «не начал за 5 минут»
            jq = getattr(ctx.application, "job_queue", None)
            if jq:
                # сначала снимаем прошлую, если вдруг была
                for job in jq.jobs():
                    if (job.name or "") == f"{uid}:nostart":
                        job.schedule_removal()

                async def _no_start_job(_ctx: ContextTypes.DEFAULT_TYPE) -> None:
                    try:
                        cur_ws = _ws_get(_ctx, uid)
                        # считаем, что стартовал, если пришло хотя бы одно фото (или вообще закончилась тренировка)
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

        # TZ пользователя
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
    """Списывает залог пользователя (однократно) и сообщает причину."""
    try:
        async with Database.acquire() as conn:
            row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id=$1", user_id)
        tf = _load_training_form(row.get("training_form") if row else None) or {}

        # уже списан — выходим
        if tf.get("deposit_forfeit"):
            return

        deposit = int(tf.get("deposit") or 0)
        if deposit <= 0:
            return

        tf["deposit_forfeit"] = True
        tf["deposit_forfeit_reason"] = str(reason)
        tf["deposit_forfeit_at"] = datetime.now(_tz_for(user_id)).isoformat()
        tf["deposit_left"] = 0

        # (опционально) чтобы в профиле было видно, что денег на кону больше нет
        # tf["deposit"] = 0

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
        "intro": st.get("intro"),
        "self_rate": st.get("self_rate"),
        "program_price": st.get("program_price"),
        "source": st.get("source"),
        "schedule": {
            "per_day_time": per_day_time,
            "per_day_duration": per_day_duration if per_day_duration else None,
            "duration_common_min": dur_common,
        },
        "rest_seconds": st.get("rest_seconds"),
        "reg_photos": list(st.get("photos") or []),
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

    # Fallback-эвристика
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

    try:
        pp = profile.get("program_price") or ""
        m = re.search(r"\d{3,6}", str(pp).replace(" ", ""))
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

# ===================== ОНБОРДИНГ =====================
def _reg_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("reg", {})

def _reg_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return "reg" in context.user_data
# --- регистрация: проверка, что уже есть оформленный training_form ---
async def _already_registered(user_id: int) -> bool:
    async with Database.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT training_form FROM users WHERE user_id = $1",
            user_id
        )
    tf = _load_training_form(row.get("training_form") if row else None)
    per_day_time = (tf or {}).get("per_day_time") or {}
    return bool(per_day_time)  # считаем «зарегистрирован», если есть расписание по дням

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user


    # 🚫 защита от повторной регистрации
    if await _already_registered(user.id):
        await msg.reply_text(
            "Ты уже прошёл онбординг — повторная регистрация не нужна.\n",
            reply_markup=_make_keyboard(False, user.id)
        )
        return
    async with Database.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, rest_seconds, timezone FROM users WHERE user_id=$1", user.id)
        if not row:
            await conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, timezone)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (user_id) DO NOTHING
                """,
                user.id, user.username, user.first_name, user.last_name, getattr(settings, "TIMEZONE", "Europe/Moscow")
            )
            _set_rest_seconds_cached(user.id, 60)
            _set_tz_for(user.id, getattr(settings, "TIMEZONE", "Europe/Moscow"))
        else:
            _set_rest_seconds_cached(user.id, int(row.get("rest_seconds") or 60))
            _set_tz_for(user.id, row.get("timezone") or getattr(settings, "TIMEZONE", "Europe/Moscow"))

    st = _reg_state(context)
    st.clear()
    st["name"] = user.first_name
    st["photos"] = []
    st["step"] = "photos"
    st["schedule_map_time"] = {}
    st["schedule_map_duration"] = {}

    pinned = await msg.reply_text("🔥🔥🔥\n*ПОМНИ СВОЮ ЦЕЛЬ*\n🔥🔥🔥", parse_mode="Markdown")
    try:
        await context.bot.pin_chat_message(chat_id=msg.chat_id, message_id=pinned.message_id)
    except Exception:
        pass

    await msg.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я — *Foscar*, твой личный тренер и строгий напарник 🥷.\n\n"
        "Сейчас ты в состоянии *неосознанного оптимизма*. Мотивация спадёт — я удержу тебя в колее ⚡",
        parse_mode="Markdown",
    )
    await msg.reply_text(
        "📸 Пришли *селфи* и *фото во весь рост* в спортивной форме (минимум 2 фото).\n"
        "Можно отправить как обычное фото или как файл-изображение.",
        parse_mode="Markdown",
    )

async def register_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _reg_active(context):
        return
    st = _reg_state(context)
    if st.get("step") != "photos":
        return

    msg = update.effective_message
    file_id = _extract_image_file_id_from_message(update.message)
    if not file_id:
        await msg.reply_text("Это не похоже на фото. Пришли фото как изображение 🙏")
        return

    st["photos"].append(file_id)
    if len(st["photos"]) >= 2:
        st["step"] = "q_intro"
        await msg.reply_text(
            f"💪 Отлично, {st.get('name','друг')}! Теперь пару слов о тебе.\n\n"
            "✍️ Расскажи:\n— Почему решил тренироваться?\n— Какая цель?\n— Есть ли опыт?"
        )
    else:
        await msg.reply_text("Ок. Пришли ещё одно фото во весь рост.")
async def _update_deposit_in_db(user_id: int, deposit: int, deposit_days: int, restart_window: bool = False) -> None:
    async with Database.acquire() as conn:
        row = await conn.fetchrow("SELECT training_form FROM users WHERE user_id=$1", user_id)
    tf = _load_training_form(row.get("training_form") if row else None) or {}

    tf["deposit"] = int(deposit)
    tf["deposit_days"] = int(deposit_days)

    if restart_window:
        tf["deposit_done_dates"] = []
        tf["deposit_started_at"] = datetime.now(_tz_for(user_id)).isoformat()
        # важно: «раз-списываем»
        tf["deposit_forfeit"] = False
        tf["deposit_forfeit_reason"] = ""
        tf["deposit_forfeit_at"] = None
        tf["deposit_left"] = int(deposit)

    async with Database.acquire() as conn:
        await conn.execute(
            "UPDATE users SET training_form=$2 WHERE user_id=$1",
            user_id, json.dumps(tf, ensure_ascii=False)
        )

async def register_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _reg_active(context):
        return
    msg = update.effective_message
    text = (msg.text or "").strip()
    st = _reg_state(context)
    name = st.get("name") or "друг"

    # 1) Вопросы анкеты
    if st.get("step") == "q_intro":
        st["intro"] = text
        st["step"] = "q_self_rate"
        await msg.reply_text(f"🎯 Хорошо, {name}. Как оцениваешь свою способность доводить цели до конца?")
        return

    if st.get("step") == "q_self_rate":
        st["self_rate"] = text
        st["step"] = "q_price"
        await msg.reply_text(f"💸 {name}, сколько стоила твоя последняя программа тренировок (если была)?")
        return

    if st.get("step") == "q_price":
        st["program_price"] = text
        st["step"] = "q_source"
        await msg.reply_text(f"🔎 И последний вопрос, {name}: как ты узнал про меня?")
        return

    # 2) Старт выбора дней — тумблеры
    if st.get("step") == "q_source":
        st["source"] = text
        st["step"] = "pick_days"
        st["chosen_days"] = []
        await msg.reply_text(
            "🗓 Выбери дни тренировок (нажимай, чтобы включать/выключать). Потом — «Готово ▶️».",
            reply_markup=_days_toggle_kb(st)
        )
        return

    # 3) Ручной ввод времени для конкретного дня
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

    # 4) Ручной ввод отдыха
    if st.get("step") == "ask_rest":
        rest_sec = _parse_rest_seconds(text)
        if rest_sec is None or rest_sec > 24*60*60:
            await msg.reply_text("Введи секунды или ММ:СС. Пример: 60 или 1:30.")
            return
        st["rest_seconds"] = rest_sec
        st["step"] = "ask_duration_mode"
        await msg.reply_text("⏲️ Длительность одинаковая или разная по дням?", reply_markup=_dur_mode_inline_kb_pretty())
        return
    if st.get("step") == "ask_deposit_custom":
        m = re.search(r"\d{1,6}", text.replace(" ", ""))
        if not m:
            await msg.reply_text("Нужно целое число в ₽. Пример: 3000")
            return
        val = _clamp_deposit(int(m.group(0)))  # 500..100000
        st["deposit"] = val
        st["step"] = "ask_deposit_days"
        st.setdefault("deposit_started_at", date.today().isoformat())
        st.setdefault("deposit_done_dates", [])
        await msg.reply_text("📅 На сколько дней заморозить залог? Введи число (1–90), например 7, 14, 21.")
        return

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

        # теперь завершаем онбординг как раньше
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
    # 5a) Ручной ввод общей длительности
    if st.get("step") == "ask_duration_common":
        dur = _parse_duration_minutes(text)
        if dur is None:
            await msg.reply_text("Введи минуты 5–240, например 60.")
            return
        st["duration_common_min"] = dur
        _set_window_seconds_cached(update.effective_user.id, int(dur)*60)
        await _auto_deposit_and_finish(msg, update, context, st)
        return

    # 5b) Ручной ввод длительности для дня
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
        await _auto_deposit_and_finish(msg, update, context, st)
        return

    # Фолбэк
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

    # порядок дней
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
        "intro": st.get("intro"),
        "self_rate": st.get("self_rate"),
        "program_price": st.get("program_price"),
        "source": st.get("source"),
        "deposit": st.get("deposit"),
        "deposit_days": st.get("deposit_days"),
        "deposit_started_at": st.get("deposit_started_at"),
        "deposit_done_dates": st.get("deposit_done_dates", []),
        "reg_photos": list(st.get("photos") or []),
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
    deposit_days = int(st.get("deposit_days") or 7)  # по умолчанию 7
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
    await _safe_cq_answer(cq)

    # ====== Выбор дней (тумблеры) ======
    if data.startswith("days_toggle:"):
        day_en = data.split(":", 1)[1]
        chosen = set(st.get("chosen_days", []))
        if day_en in chosen:
            chosen.remove(day_en)
        else:
            chosen.add(day_en)
        st["chosen_days"] = list(chosen)
        await _safe_edit_reply_markup(cq.message, _days_toggle_kb(st))  # ✅ безопасно
        await _safe_cq_answer(cq)
        return

    if data == "days_clear":
        st["chosen_days"] = []
        await _safe_edit_reply_markup(cq.message, _days_toggle_kb(st))  # ✅ безопасно
        await _safe_cq_answer(cq, "Сброшено")
        return
    if data == "dep_ok":
        # оставляем st["deposit"] как есть и идём спрашивать срок заморозки
        st["step"] = "ask_deposit_days"
        await cq.message.reply_text("📅 На сколько дней заморозить залог? Введи число (1–90), например 7, 14, 21.")
        await _safe_cq_answer(cq, "Ок")
        return

    if data == "dep_custom":
        # просим ввести свою сумму
        st["step"] = "ask_deposit_custom"
        await cq.message.reply_text("✏️ Введи свою сумму залога (₽). Допускается только число, диапазон 500–100000.")
        await _safe_cq_answer(cq, "Введи свою сумму")
        return
    if data == "days_done":
        chosen = [d for d in ORDERED_DAYS if d in set(st.get("chosen_days", []))]
        if not chosen:
            await _safe_cq_answer(cq,"Выбери хотя бы один день", show_alert=True)
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
        # callback_data = "time_pick:<day_en>:<HH:MM>"
        parts = data.split(":", 2)  # важное отличие: maxsplit=2
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
            await _safe_edit_reply_markup(cq.message, _dur_common_kb(v))  # ✅ безопасно
        await _safe_cq_answer(cq, f"{v} мин")
        return

    if data.startswith("dur_common_adj:"):
        delta = int(data.split(":", 1)[1])
        cur = int(st.get("duration_common_min") or 60)
        v = max(5, min(240, cur + delta))
        if v != cur:
            st["duration_common_min"] = v
            await _safe_edit_reply_markup(cq.message, _dur_common_kb(v))  # ✅ безопасно
        await _safe_cq_answer(cq, f"{v} мин")
        return

    if data == "dur_common_custom":
        st["step"] = "ask_duration_common"
        await cq.message.reply_text("Введи общее количество минут (5–240):")
        await _safe_cq_answer(cq)
        return

    if data == "dur_common_done":
        await _safe_cq_answer(cq, "Готово")
        v = int(st.get("duration_common_min") or 60)
        _set_window_seconds_cached(update.effective_user.id, int(v)*60)
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
        try:
            any_dur = next(iter(st.get("schedule_map_duration", {}).values()))
        except Exception:
            any_dur = 60
        _set_window_seconds_cached(update.effective_user.id, int(any_dur)*60)
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

    await _safe_cq_answer(cq)

# ---------------- Хендлеры ----------------
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

    if _is_admin(user.id):
        if low in ("🟢 старт тренировки (админ)", "старт тренировки", "🟢 старт тренировки", "/start_workout"):
            _set_session_active(context, user.id, True)
            _ws_reset(context, user.id)
            _ws_get(context, user.id)
            await message.reply_text(
                "🚀 Режим тренировки включён (админ). Жми «▶️ Начать тренировку». Будет 3 снимка с паузами отдыха.",
                reply_markup=_make_keyboard(True, user.id)
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
    if context.user_data.get("dep_edit"):
        st = context.user_data["dep_edit"]

        # ждём сумму
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

        # ждём дни
        if st.get("await") == "days":
            m = re.search(r"\d{1,3}", msg)
            if not m:
                await message.reply_text("Введи просто число дней (1–90).")
                return
            days = int(m.group(0))
            if not (1 <= days <= 90):
                await message.reply_text("Число вне диапазона. Разрешено 1–90 дней.")
                return

            # Сохраняем в training_form и сбрасываем прогресс
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
    # мастер настройки напоминаний (общий случай)
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

    # NEW в handle_text(): шаг 2 — ждём срок и применяем изменения
    if context.user_data.get("awaiting_dep_days"):
        m = re.search(r"\d{1,3}", msg)
        if not m:
            await message.reply_text("Введи число дней 1–90. Пример: 21")
            return
        days = max(1, min(90, int(m.group(0))))
        amount = int(context.user_data.get("new_deposit_amount"))
        context.user_data.pop("awaiting_dep_days", None)
        context.user_data.pop("new_deposit_amount", None)

        # применяем и сразу перезапускаем окно
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

# ---------------- Приём данных из WebApp ----------------
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.web_app_data:
        return
    try:
        raw = update.message.web_app_data.data
        payload = json.loads(raw)
    except Exception:
        logger.exception("[webapp] failed to parse web_app_data JSON")
        return

    ptype = str(payload.get("type"))  # ← сначала определяем ptype

    if ptype in ("single_photo_uploaded", "set_photo_uploaded"):
        user = update.effective_user
        ...
        ok = await _save_training_photo(user.id, photo_bytes, context.bot, notify=False)
        ws = _ws_get(context, user.id)
        ws["results"].append(ok)

        # отменяем «не начал за 5 минут», раз фото пришло
        jq = getattr(context.application, "job_queue", None)
        if jq:
            for job in jq.jobs():
                if (job.name or "") == f"{user.id}:nostart":
                    job.schedule_removal()
    try:
        raw = update.message.web_app_data.data
        payload = json.loads(raw)
    except Exception:
        logger.exception("[webapp] failed to parse web_app_data JSON")
        return

    ptype = str(payload.get("type"))

    # ====== автоопределение TZ через WebApp ======
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

        # Перепланируем напоминания под новую зону (если включены)
        await _reschedule_from_db(update, context, user.id)
        await update.message.reply_text(f"🕒 Часовой пояс обновлён: {tz_name}")
        return

    # ====== загрузка одиночного фото ======
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

        if len(ws["results"]) >= ws["expected"]:
            await _finalize_workout(context, user.id, ws["results"])
            _ws_reset(context, user.id)
            _set_session_active(context, user.id, False)
        else:
            await update.message.reply_text(f"Фото получено ({len(ws['results'])}/3). Продолжаем…")
        return

    # ====== загрузка сета фото ======
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
from io import BytesIO

async def _finalize_workout(context: ContextTypes.DEFAULT_TYPE, user_id: int, results: List[bool]) -> None:
    # Сколько ожидалось/получено/подтверждено
    ws = _ws_get(context, user_id)
    expected = int(ws.get("expected", 3))
    received = len(results)
    verified = sum(1 for x in results if x)

    # Порог зачёта: для 3 снимков допускаем 1 ошибку → 2 из 3 — зачёт.
    # Для <3 — требуем все (1/1 или 2/2).
    threshold = expected - 1 if expected >= 3 else expected

    # Снять страховочные таймеры (не начал / нет результата)
    def _cancel_timers():
        jq = getattr(context.application, "job_queue", None)
        if jq:
            for job in jq.jobs():
                if (job.name or "") in (f"{user_id}:nostart", f"{user_id}:noresult"):
                    job.schedule_removal()

    if verified >= threshold:
        # ✅ Засчитано
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

        # Обновляем прогресс по заморозке (добавляем сегодняшнюю дату в done_dates)
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

                # Всё выполнено — показать меню действий
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

    # ❌ Не засчитано — недостаточно подтверждённых фото
    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "🏁 Тренировка: ❌ не засчитана.\n"
            f"Получено: {received}/{expected}, подтверждено: {verified}/{expected}.\n"
            f"Для зачёта нужно ≥ {threshold}."
        )
    )

    # Покажем последние фото с причинами отказов
    await _send_last_photos_with_reasons(context, user_id, limit=expected)

    # Подсказка по обжалованию
    try:
        admin_username = getattr(settings, "ADMIN_USERNAME", None)
        if admin_username:
            await context.bot.send_message(chat_id=user_id, text=f"💬 Обжалование: напиши @{admin_username}.")
        else:
            await context.bot.send_message(chat_id=user_id, text="💬 Обжалование: напиши администратору.")
    except Exception:
        pass

    # Списываем залог, если ещё не списан
    await _forfeit_deposit(context, user_id, f"недостаточно подтверждённых фото ({verified}/{expected})")

    _cancel_timers()

from io import BytesIO
from telegram import InputMediaPhoto

async def _send_last_photos_with_reasons(context: ContextTypes.DEFAULT_TYPE, user_id: int, limit: int = 3) -> None:
    """
    Достаёт из БД последние N фото пользователя и шлёт их ему:
    - одним медиа-группом (если получится),
    - или по одному (fallback).
    К каждому фото добавляем подпись с вердиктом и reason от GPT.
    """
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

    # Готовим подписи
    def _cap(verified: bool, reason: str, idx: int) -> str:
        status = "❌ Не засчитано" if not verified else "✅ Засчитано"
        reason = (reason or "").strip()
        if reason:
            return f"{idx}. {status}\nПричина: {reason}"
        return f"{idx}. {status}"

    # Пытаемся отправить медиа-группой (с короткими подписями до ~1024 символов)
    media: List[InputMediaPhoto] = []
    for i, r in enumerate(rows[::-1], start=1):  # в хронологическом порядке
        b = bytes(r.get("photo") or b"")
        cap = _cap(bool(r.get("verified")), str(r.get("gpt_reason") or ""), i)
        try:
            media.append(
                InputMediaPhoto(media=b, caption=cap[:1024])
            )
        except Exception:
            # Если по каким-то причинам bytes не принялись — отправим по одному ниже
            media = []
            break

    if media:
        try:
            await context.bot.send_media_group(chat_id=user_id, media=media)
            return
        except Exception as e:
            logger.exception("send_media_group failed, will fallback to singles: %s", e)

    # Fallback: шлём по одному
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

async def deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data or ""
    user = update.effective_user
    await _safe_cq_answer(cq)

    # Достаём текущую анкету
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
        tf["deposit_done_dates"] = []              # список ISO дат выполненных трен-дней
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

    # Меняем только залог: запускаем мини-мастер через handle_text (state = dep_edit)
    if data == "depwin_change_amount":
        context.user_data["dep_edit"] = {"await": "amount"}  # ждём сумму
        await cq.message.reply_text("✏️ Введи новую сумму залога (₽). Диапазон 500–100000.")
        return

    # Меняем расписание: запускаем уже готовый мастер напоминаний
    if data == "depwin_change_sched":
        await reminders(update, context)
        return
    if data == "depforf_restart":
        amount = int(tf.get("deposit") or 0)
        days = int(tf.get("deposit_days") or 0)

        # если нет суммы или срока — запускаем мини-мастер изменения залога
        if amount <= 0:
            context.user_data["dep_edit"] = {"await": "amount"}
            await cq.message.reply_text("✏️ Введи новую сумму залога (₽). Диапазон 500–100000.")
            return
        if not (1 <= days <= 90):
            context.user_data["dep_edit"] = {"await": "days", "amount": amount}
            await cq.message.reply_text("📅 На сколько дней заморозить залог? Введи число 1–90 (например 7, 14, 21).")
            return

        # полноценный рестарт окна: «раз-списываем», обнуляем прогресс, ставим новую дату старта
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
        reply_markup=_make_keyboard(True, user.id)
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

# ---------------- Профиль ----------------
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def _progress_bar(done: int, total: int, width: int = 20) -> str:
        if total <= 0:
            return "▱" * width
        filled = max(0, min(width, (done * width) // total))
        return "▰" * filled + "▱" * (width - filled)

    def _iso_to_local_str(iso_str: Optional[str], tz: ZoneInfo) -> Optional[str]:
        if not iso_str:
            return None
        try:
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return html.escape(str(iso_str))

    message = update.message or update.callback_query.message
    user = update.effective_user

    reminder_enabled = False
    rtime: Optional[time] = None
    duration_global: Optional[int] = None
    per_day_time: Dict[str, str] = {}
    per_day_duration: Dict[str, int] = {}
    rest_seconds: Optional[int] = None

    # анкета
    intro = None
    self_rate = None
    program_price = None
    source = None
    deposit = None
    deposit_days = None
    deposit_started_at = None
    deposit_done_dates: List[str] = []
    reg_photos: List[str] = []

    planned_week = 0
    completed_week = 0
    tf: Dict = {}

    try:
        async with Database.acquire() as conn:
            row_user = await conn.fetchrow(
                """
                SELECT username, first_name, last_name,
                       reminder_enabled, reminder_days, reminder_time,
                       workout_duration, rest_seconds, training_form, registration_date, timezone
                  FROM users
                 WHERE user_id = $1
                """,
                user.id
            )
            if row_user:
                # TZ
                tz_name = row_user.get("timezone") or getattr(settings, "TIMEZONE", "Europe/Moscow")
                _set_tz_for(user.id, tz_name)

                reminder_enabled = bool(row_user["reminder_enabled"])
                rtime = row_user["reminder_time"]
                duration_global = row_user["workout_duration"]
                rest_seconds = row_user.get("rest_seconds")
                _set_rest_seconds_cached(user.id, int(rest_seconds or 60))

                tf = _load_training_form(row_user.get("training_form"))
                per_day_time = tf.get("per_day_time") or {}
                per_day_duration = tf.get("per_day_duration") or {}

                intro = tf.get("intro")
                self_rate = tf.get("self_rate")
                program_price = tf.get("program_price")
                source = tf.get("source")
                deposit = tf.get("deposit")
                deposit_days = tf.get("deposit_days")
                deposit_started_at = tf.get("deposit_started_at")  # ISO-строка
                deposit_done_dates = list(tf.get("deposit_done_dates") or [])
                reg_photos = list(tf.get("reg_photos") or [])

                if per_day_time:
                    planned_week = len(per_day_time)
                else:
                    try:
                        rdays = row_user.get("reminder_days") or []
                        planned_week = len([d for d in rdays if d in ORDERED_DAYS])
                    except Exception:
                        planned_week = 0

            # Выполненные за 7 дней (по локальному TZ пользователя)
            tz = _tz_for(user.id)
            dt_to = datetime.now(tz)
            dt_from = dt_to - timedelta(days=7)

            rows_sets = []
            try:
                rows_sets = await conn.fetch(
                    """
                    SELECT created_at, verified
                      FROM sets
                     WHERE user_id = $1
                       AND created_at >= $2
                    """,
                    user.id,
                    dt_from.astimezone(dt_timezone.utc)
                )
            except Exception:
                try:
                    rows_sets = await conn.fetch(
                        """
                        SELECT ts AS created_at, verified
                          FROM sets
                         WHERE user_id = $1
                           AND ts >= $2
                        """,
                        user.id,
                        dt_from.astimezone(dt_timezone.utc)
                    )
                except Exception:
                    rows_sets = []

            completed_days = set()
            for r in rows_sets:
                if not bool(r.get("verified")):
                    continue
                ts = r.get("created_at")
                if not isinstance(ts, datetime):
                    continue
                ts_local = ts.astimezone(tz) if ts.tzinfo else ts.replace(tzinfo=tz)
                completed_days.add(ts_local.date())

            completed_week = len(completed_days)

    except Exception as e:
        logger.exception("profile() failed: %s", e)

    # Наглядная недельная шкала (оставил — полезно видеть общую дисциплину)
    percent_week = int((completed_week / planned_week) * 100) if planned_week else 0
    week_bar = _progress_bar(completed_week, planned_week)

    now_local = datetime.now(_tz_for(user.id))
    tz_label = getattr(_tz_for(user.id), "key", str(_tz_for(user.id)))
    today_line = now_local.strftime(f"%Y-%m-%d (%A) %H:%M")

    if per_day_time:
        sched_lines = _human_schedule_lines(per_day_time, per_day_duration or None)
        sched_text = "\n".join(sched_lines)
    else:
        sched_text = (
            f"Время: {rtime.strftime('%H:%M') if rtime else '—'}\n"
            f"Длительность: {f'{duration_global} мин.' if duration_global else '—'}"
        )
    _set_registered(user.id, bool(per_day_time))
    rest_text = f"{rest_seconds} сек." if rest_seconds is not None else "—"

    # Анкета
    form_bits = []
    if intro: form_bits.append(f"• Цель/почему: {_h(intro)}")
    if self_rate: form_bits.append(f"• Дисциплина: {_h(self_rate)}")
    if program_price: form_bits.append(f"• Последняя программа: {_h(program_price)}")
    if source: form_bits.append(f"• Как узнал: {_h(source)}")
    form_bits.append(_format_deposit_status(tf, _tz_for(user.id)))
    if (tf.get("deposit") is not None) and (deposit_days is not None):
        form_bits.append(f"• Срок заморозки: {deposit_days} дн.")
    form_text = "\n".join(form_bits) if form_bits else "—"

    # Прогресс по заморозке
    dep_days_total = int(deposit_days or 0)
    dep_done = len(deposit_done_dates or [])
    deposit_section = ""
    try:
        # показываем прогресс ТОЛЬКО если залог не списан
        if dep_days_total > 0 and not bool(tf.get("deposit_forfeit")):
            percent_dep = int(dep_done * 100 / dep_days_total)
            started_str = _iso_to_local_str(deposit_started_at, _tz_for(user.id))
            deposit_section = (
                    f"<b>💰 Прогресс по заморозке</b>\n"
                    f"{dep_done}/{dep_days_total} ({percent_dep}%)\n"
                    f"{_progress_bar(dep_done, dep_days_total)}"
                    + (f"\nСтарт окна: {html.escape(started_str)}" if started_str else "")
                    + "\n\n"
            )
    except Exception:
        deposit_section = ""

    html_text = (
        f"<b>👤 Профиль @{_h(user.username) if user.username else user.id}</b>\n"
        f"{_h(today_line)} ({_h(tz_label)})\n\n"
        f"🔔 Напоминания: <b>{'включены' if reminder_enabled else 'выключены'}</b>\n\n"
        f"<b>Дни/время/длительность</b>\n{sched_text}\n\n"
        f"<b>Отдых</b>: {rest_text}\n\n"
        f"<b>📝 Анкета</b>\n{form_text}\n\n"
        f"{deposit_section}"
        f"Режим тренировки: <b>{'активен' if _is_session_active(context, user.id) else 'выключен'}</b>"
    )

    # Показ фото анкеты (если есть)
    if reg_photos:
        media = [InputMediaPhoto(p) for p in reg_photos[:10]]
        try:
            await context.bot.send_media_group(chat_id=user.id, media=media)
        except Exception as e:
            logger.exception("send_media_group failed: %s", e)
            try:
                await context.bot.send_photo(chat_id=user.id, photo=reg_photos[0])
            except Exception:
                pass

    # Отправка профиля + выбор клавиатуры по прогрессу заморозки
    reply_markup = _current_keyboard(context, user.id)
    try:
        if bool(tf.get("deposit_forfeit")):
            # залог списан — предлагаем рестарт
            await message.reply_text(
                html_text, parse_mode="HTML",
                reply_markup=_deposit_forfeit_kb()
            )
        elif dep_days_total > 0 and dep_done >= dep_days_total:
            # окно выполнено — показываем действия по завершению
            await message.reply_text(
                html_text, parse_mode="HTML",
                reply_markup=_deposit_complete_kb()
            )
        else:
            await message.reply_text(
                html_text, parse_mode="HTML",
                reply_markup=reply_markup
            )
        return
    except Exception:
        pass
    await message.reply_text(html_text, parse_mode="HTML", reply_markup=reply_markup)
