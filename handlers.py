# handlers.py
import logging
import re
import json
from datetime import datetime, timedelta, time, date
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

from database import Database
from gpt_tasks import verify_task_with_gpt
from config import settings

from zoneinfo import ZoneInfo
APP_TZ = ZoneInfo(getattr(settings, "TIMEZONE", "Europe/Moscow"))

logger = logging.getLogger(__name__)

# ======================= КЭШ/СЕССИИ =======================
REST_CACHE: dict[int, int] = {}          # user_id -> rest_seconds (для URL WebApp)
WORKOUT_WINDOW_CACHE: dict[int, int] = {}  # user_id -> seconds (длительность окна тренировки)

def _get_rest_seconds_cached(user_id: int) -> int:
    return int(REST_CACHE.get(user_id, 60))

def _set_rest_seconds_cached(user_id: int, seconds: int) -> None:
    REST_CACHE[user_id] = max(1, int(seconds))

def _get_window_seconds_cached(user_id: int) -> int:
    return int(WORKOUT_WINDOW_CACHE.get(user_id, 3600))

def _set_window_seconds_cached(user_id: int, seconds: int) -> None:
    WORKOUT_WINDOW_CACHE[user_id] = max(60, int(seconds))

def _ws_get(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    all_ws = context.application.bot_data.setdefault("workout_session", {})
    ws = all_ws.get(user_id)
    if not ws:
        ws = {"expected": 3, "results": [], "started_at": datetime.now(APP_TZ)}
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

# ---------------- Клавиатуры ----------------
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
    rows.append([KeyboardButton("📊 Профиль")])

    if _is_admin(user_id):
        rows.append([KeyboardButton("🟢 Старт тренировки (админ)"),
                     KeyboardButton("🔴 Стоп тренировки (админ)")])
        rows.append([KeyboardButton("/delete_db"), KeyboardButton("/clear_db")])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

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

def _add_minutes_to_time(t: time, minutes: int) -> Tuple[time, int]:
    base = datetime.combine(date(2000, 1, 3), time(t.hour, t.minute, t.second, t.microsecond, tzinfo=APP_TZ))
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

    day_idx = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}

    for d in ORDERED_DAYS:
        if d not in per_day_time or d not in day_idx:
            continue

        hhmm = per_day_time[d]
        t = _parse_time_hhmm(hhmm)
        if not t:
            logger.warning("[sched] skip day=%s invalid time=%r", d, hhmm)
            continue
        t_z = time(t.hour, t.minute, t.second, t.microsecond, tzinfo=APP_TZ)

        dur = int((per_day_duration or {}).get(d, default_duration_min))
        if dur < 1:
            dur = default_duration_min

        mid_t, mid_shift = _add_minutes_to_time(t_z, max(dur // 2, 1))
        end_t, end_shift = _add_minutes_to_time(t_z, dur)

        base_day = (day_idx[d]+1) % 7
        mid_day = (base_day + mid_shift) % 7
        end_day = (base_day + end_shift) % 7

        async def start_cb(ctx: ContextTypes.DEFAULT_TYPE, uid=user_id):
            _set_session_active(ctx, uid, True)
            _ws_reset(ctx, uid)
            _ws_get(ctx, uid)
            try:
                await ctx.bot.send_message(
                    chat_id=uid,
                    text="🏁 Старт! Жми «▶️ Начать тренировку». Будет 3 снимка с паузами отдыха.",
                    reply_markup=_make_keyboard(True, uid)
                )
            except Exception:
                logger.exception("Failed to send START reminder")

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
                SELECT reminder_enabled, reminder_days, reminder_time, workout_duration, rest_seconds, training_form
                  FROM users
                 WHERE user_id = $1
                """,
                user_id
            )
        if not row:
            return

        _set_rest_seconds_cached(user_id, int((row.get("rest_seconds") or 60)))

        if not row["reminder_enabled"]:
            _clear_user_jobs(context, user_id)
            _set_session_active(context, user_id, False)
            return

        default_dur = int(row.get("workout_duration") or 60)
        _set_window_seconds_cached(user_id, default_dur * 60)  # окно = длительность тренировки (мин * 60)

        tf = _load_training_form(row.get("training_form"))
        per_day_time = tf.get("per_day_time") or {}
        per_day_duration = tf.get("per_day_duration") or None

        if per_day_time:
            _schedule_reminders_per_day(context, user_id, per_day_time, per_day_duration, default_duration_min=default_dur)

    except Exception as e:
        logger.exception("_reschedule_from_db failed: %s", e)

# ===================== AI-залог =====================
try:
    # ожидается: async def recommend_deposit_with_gpt(profile: dict) -> dict
    # возвращает {"deposit": int, "reason": str}
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
    st["deposit"] = dep

    try:
        await wait_msg.edit_text(f"🧮 Рекомендуемый залог: *{dep} ₽*\nПричина: {why}", parse_mode="Markdown")
    except Exception:
        await message.reply_text(f"🧮 Рекомендуемый залог: *{dep} ₽*\nПричина: {why}", parse_mode="Markdown")

    await _reg_finish(message, st)
    save_text = await _persist_onboarding_schedule_per_day(user.id, context, st)
    if save_text:
        await message.reply_text(save_text)

    context.user_data.pop("reg", None)
    await message.reply_text(
        "Готово! Ниже — главное меню.",
        reply_markup=_make_keyboard(False, user.id)
    )

# ===================== ОНБОРДИНГ =====================
def _reg_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("reg", {})

def _reg_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return "reg" in context.user_data

def _days_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Пн", callback_data="day_Понедельник"),
            InlineKeyboardButton("Вт", callback_data="day_Вторник"),
            InlineKeyboardButton("Ср", callback_data="day_Среда"),
        ],
        [
            InlineKeyboardButton("Чт", callback_data="day_Четверг"),
            InlineKeyboardButton("Пт", callback_data="day_Пятница"),
            InlineKeyboardButton("Сб", callback_data="day_Суббота"),
        ],
        [InlineKeyboardButton("Вс", callback_data="day_Воскресенье")],
    ])

def _dur_mode_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Да, одинаковая", callback_data="dur_same"),
            InlineKeyboardButton("Разная по дням", callback_data="dur_diff")
        ]
    ])

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user

    async with Database.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, rest_seconds FROM users WHERE user_id=$1", user.id)
        if not row:
            await conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (user_id) DO NOTHING
                """,
                user.id, user.username, user.first_name, user.last_name
            )
            _set_rest_seconds_cached(user.id, 60)
        else:
            _set_rest_seconds_cached(user.id, int(row.get("rest_seconds") or 60))

    st = _reg_state(context)
    st.clear()
    st["name"] = user.first_name
    st["photos"] = []
    st["step"] = "photos"
    st["schedule"] = []
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

    if st.get("step") == "q_source":
        st["source"] = text
        st["step"] = "pick_day"
        await msg.reply_text(
            f"🗓 Отлично, {name}! Теперь составим план.\n\n"
            "Выбирай *день недели* кнопками. Сразу после выбора дня введёшь *время* (например 18:00).",
            parse_mode="Markdown",
            reply_markup=_days_inline_kb()
        )
        return

    # 2) Ввод времени для указанного дня
    if st.get("step") == "enter_time" and "temp_day" in st:
        t = _parse_time_hhmm(text.replace(" ", "").replace(".", ":"))
        if not t:
            await msg.reply_text("Напиши время в формате ЧЧ:ММ, например 18:00.")
            return
        day_ru = st.pop("temp_day")
        day_en = RU_FULL_TO_EN.get(day_ru.lower())
        if not day_en:
            await msg.reply_text("Не смог распознать день. Выбери кнопкой ещё раз, пожалуйста.", reply_markup=_days_inline_kb())
            st["step"] = "pick_day"
            return

        hhmm = t.strftime("%H:%M")
        st["schedule_map_time"][day_en] = hhmm

        shown = "\n".join(_human_schedule_lines(st["schedule_map_time"]))
        await msg.reply_text(
            f"✅ Записал: {day_ru} — {hhmm}.\n\n"
            f"Текущее расписание:\n{shown}\n\n"
            "Добавишь ещё день? Жми кнопку или напиши *готово*.",
            parse_mode="Markdown",
            reply_markup=_days_inline_kb()
        )
        st["step"] = "pick_day_or_done"
        return

    # 3) Завершили дни — спросим отдых
    if st.get("step") in ("pick_day_or_done", "pick_day") and text.lower() == "готово":
        if not st.get("schedule_map_time"):
            await msg.reply_text("Нужно указать хотя бы один день. Выбери день кнопкой ниже.",
                                 reply_markup=_days_inline_kb())
            return
        st["step"] = "ask_rest"
        await msg.reply_text(
            "⏱️ Сколько отдыха между подходами? Введи в секундах (например 60) или ММ:СС (например 1:30).",
            reply_markup=rest_keyboard()
        )
        return

    # 4) Отдых введён — выбираем режим длительности
    if st.get("step") == "ask_rest":
        rest_sec = _parse_rest_seconds(text)
        if rest_sec is None or rest_sec > 24 * 60 * 60:
            await msg.reply_text("Не понял. Введи число секунд (например 60) или ММ:СС (например 1:30).",
                                 reply_markup=rest_keyboard())
            return
        st["rest_seconds"] = rest_sec
        st["step"] = "ask_duration_mode"
        await msg.reply_text(
            "⏲️ Длительность тренировки одинаковая на все выбранные дни?",
            reply_markup=_dur_mode_inline_kb()
        )
        return

    # 5a) Общая длительность
    if st.get("step") == "ask_duration_common":
        dur = _parse_duration_minutes(text)
        if dur is None:
            await msg.reply_text("Введи длительность в минутах от 5 до 240, например 60.",
                                 reply_markup=duration_keyboard())
            return
        st["duration_common_min"] = dur

        # окно тренировки = длительность (мин) * 60 — сразу положим в кэш
        _set_window_seconds_cached(update.effective_user.id, int(dur) * 60)

        # вместо ручного выбора залога — автоматически считаем ИИ и завершаем
        await _auto_deposit_and_finish(msg, update, context, st)
        return

    # 5b) Пер-дневная длительность
    if st.get("step") == "ask_duration_for_day":
        pending: List[str] = st.get("pending_days", [])
        if not pending:
            # если всё заполнено — окно берём из первой длительности (или 60)
            try:
                any_dur = next(iter((st.get("schedule_map_duration") or {}).values()), 60)
            except Exception:
                any_dur = 60
            _set_window_seconds_cached(update.effective_user.id, int(any_dur) * 60)

            # считаем залог ИИ и завершаем
            await _auto_deposit_and_finish(msg, update, context, st)
            return

        current_day = pending[0]
        dur = _parse_duration_minutes(text)
        if dur is None:
            await msg.reply_text("Введи длительность в минутах от 5 до 240, например 60.",
                                 reply_markup=duration_keyboard())
            return

        st["schedule_map_duration"][current_day] = dur
        pending.pop(0)

        if pending:
            ru_next = EN_TO_RU_FULL.get(pending[0], pending[0])
            await msg.reply_text(
                f"⏲️ Сколько минут тренируешься по {ru_next.lower()}?",
                reply_markup=duration_keyboard()
            )
        else:
            # аналогично — окно берём из первой длительности (или 60)
            try:
                any_dur = next(iter((st.get("schedule_map_duration") or {}).values()), 60)
            except Exception:
                any_dur = 60
            _set_window_seconds_cached(update.effective_user.id, int(any_dur) * 60)

            await _auto_deposit_and_finish(msg, update, context, st)
        return

    if st.get("step") in ("pick_day", "pick_day_or_done"):
        await msg.reply_text("Выбирай день кнопкой ниже или напиши *готово*.",
                             parse_mode="Markdown", reply_markup=_days_inline_kb())

async def _persist_onboarding_schedule_per_day(user_id: int, context: ContextTypes.DEFAULT_TYPE, st: dict) -> Optional[str]:
    per_day_time: Dict[str, str] = st.get("schedule_map_time") or {}
    if not per_day_time:
        return None

    # порядок дней для детерминированности
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

    # для WebApp окна — берём общую длительность или первую по дням
    dur_for_window = workout_duration_common
    if dur_for_window is None:
        try:
            dur_for_window = int(next(iter(per_day_duration.values())))
        except StopIteration:
            dur_for_window = 60
    _set_window_seconds_cached(user_id, int(dur_for_window) * 60)

    # для совместимости: одно "главное" время
    first_time_val: Optional[str] = None
    for d in ORDERED_DAYS:
        if d in per_day_time:
            first_time_val = per_day_time[d]
            break
    rtime: Optional[time] = _parse_time_hhmm(first_time_val) if first_time_val else None

    reminder_days = list(per_day_time.keys())

    # 🔹 собираем все ответы онбординга и file_id фото
    extras = {
        "intro": st.get("intro"),
        "self_rate": st.get("self_rate"),
        "program_price": st.get("program_price"),
        "source": st.get("source"),
        "deposit": st.get("deposit"),
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
    schedule = _reg_schedule_text_lines(st)
    rest_seconds = int(st.get("rest_seconds") or 60)
    await msg.reply_text(
        f"🚀 Отлично, {name}! Мы замораживаем {dep} ₽ на 7 дней.\n\n"
        "Если выполнишь все тренировки — деньги полностью вернутся ✅\n\n"
        "Если пропустишь:\n"
        "— 1-й пропуск — 500 ₽\n"
        "— 2-й пропуск — 1270 ₽\n"
        "— 3-й пропуск — 3230 ₽\n\n"
        f"Твоё расписание:\n{schedule}\n"
        f"Отдых между подходами: {rest_seconds} сек."
    )

async def register_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _reg_active(context):
        await update.callback_query.answer()
        return

    cq = update.callback_query
    data = cq.data or ""
    st = _reg_state(context)

    if data.startswith("day_"):
        day_ru = data.split("_", 1)[1]
        st["temp_day"] = day_ru
        st["step"] = "enter_time"
        await cq.message.reply_text(f"⏰ Хорошо, {day_ru}. Теперь напиши *время* (например 18:00).", parse_mode="Markdown")
        await cq.answer()
        return

    if data in ("dur_same", "dur_diff"):
        if data == "dur_same":
            st["dur_mode"] = "same"
            st["step"] = "ask_duration_common"
            await cq.message.reply_text("Введи общую длительность тренировки (минуты, 5–240), например 60.",
                                        reply_markup=duration_keyboard())
        else:
            st["dur_mode"] = "per_day"
            st["step"] = "ask_duration_for_day"
            per_day_time: Dict[str, str] = st.get("schedule_map_time") or {}
            pending = [d for d in ORDERED_DAYS if d in per_day_time]
            st["pending_days"] = pending
            ru = EN_TO_RU_FULL.get(pending[0], pending[0])
            await cq.message.reply_text(
                f"⏲️ Сколько минут тренируешься по {ru.lower()}?",
                reply_markup=duration_keyboard()
            )
        await cq.answer()
        return

    # депозита тут больше нет
    await cq.answer()

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

    if _reg_active(context):
        await register_text(update, context)
        return

    msg = message.text.strip()
    low = msg.lower()
    user = update.effective_user

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

        # также положим окно в кэш из глобальной длительности
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

    ptype = str(payload.get("type"))

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
    passed_count = sum(1 for x in results if x)
    if passed_count > 0:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🏁 Тренировка завершена: ✅ пройдена (засчитано фото: {passed_count}/3)."
        )
    else:
        await context.bot.send_message(
            chat_id=user_id,
            text="🏁 Тренировка завершена: ❌ не прошёл (на всех фото нет выполнения упражнения)."
        )

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
    message = update.message or update.callback_query.message
    user = update.effective_user

    total_tasks = 0
    completed_tasks = 0
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
    reg_photos: List[str] = []

    try:
        async with Database.acquire() as conn:
            try:
                row_stats = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
                      FROM tasks
                     WHERE user_id = $1
                    """,
                    user.id
                )
                if row_stats:
                    total_tasks = int(row_stats["total"] or 0)
                    completed_tasks = int(row_stats["completed"] or 0)
            except Exception:
                pass

            row_user = await conn.fetchrow(
                """
                SELECT username, first_name, last_name,
                       reminder_enabled, reminder_days, reminder_time,
                       workout_duration, rest_seconds, training_form, registration_date
                  FROM users
                 WHERE user_id = $1
                """,
                user.id
            )
            if row_user:
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
                reg_photos = list(tf.get("reg_photos") or [])

    except Exception as e:
        logger.exception("profile() failed: %s", e)

    percent = int((completed_tasks / total_tasks) * 100) if total_tasks else 0

    now_local = datetime.now(APP_TZ)
    tz_label = getattr(APP_TZ, "key", str(APP_TZ))
    today_line = now_local.strftime(f"Сегодня: %Y-%m-%d (%A) %H:%M ({tz_label})")

    lines = [
        f"👤 Профиль @{user.username or user.id}",
        today_line,
        f"Задач: {total_tasks}, выполнено: {completed_tasks} ({percent}%)",
        "",
        "🔔 Напоминания: " + ("включены" if reminder_enabled else "выключены"),
    ]

    # Расписание
    if per_day_time:
        lines.append("Дни/время/длительность:")
        lines += _human_schedule_lines(per_day_time, per_day_duration or None)
    else:
        lines.append("Время: " + (rtime.strftime('%H:%M') if rtime else "—"))
        lines.append("Длительность: " + (f"{duration_global} мин." if duration_global else "—"))

    if rest_seconds is not None:
        lines.append(f"Отдых между подходами: {rest_seconds} сек.")

    # Анкетные ответы
    lines.append("")
    lines.append("📝 Анкета:")
    if intro:
        lines.append(f"• Цель/почему: {intro}")
    if self_rate:
        lines.append(f"• Самооценка дисциплины: {self_rate}")
    if program_price:
        lines.append(f"• Последняя программа: {program_price}")
    if source:
        lines.append(f"• Как узнал: {source}")
    if deposit is not None:
        lines.append(f"• Залог: {deposit} ₽")

    # Режим
    lines.append("")
    lines.append("Режим тренировки: " + ("активен" if _is_session_active(context, user.id) else "выключен"))

    # Сначала фото (если есть), затем — текст
    if reg_photos:
        media = [InputMediaPhoto(p) for p in reg_photos[:10]]  # лимит медиагруппы Telegram — 10
        try:
            await context.bot.send_media_group(chat_id=user.id, media=media)
        except Exception as e:
            logger.exception("send_media_group failed: %s", e)
            # запасной вариант — первое фото
            try:
                await context.bot.send_photo(chat_id=user.id, photo=reg_photos[0])
            except Exception:
                pass

    await message.reply_text("\n".join(lines), reply_markup=_current_keyboard(context, user.id))

# Совместимость со старым именем
handle_photo = register_photo
