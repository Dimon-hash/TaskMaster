# handlers.py
import logging
import re
import json
from datetime import datetime, timedelta, time
from typing import List, Optional

import aiohttp
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from telegram.ext import ContextTypes

from database import Database
from gpt_tasks import verify_task_with_gpt  # опционально, используем для фото-проверки сетов
from config import settings

from zoneinfo import ZoneInfo
APP_TZ = ZoneInfo(getattr(settings, "TIMEZONE", "Europe/Moscow"))

logger = logging.getLogger(__name__)

# ---------------- Утилиты ----------------
def _is_admin(user_id: int) -> bool:
    """Проверка прав админа."""
    try:
        if user_id == getattr(settings, "ADMIN_ID", 0):
            return True
        admin_ids = set(getattr(settings, "ADMIN_IDS", []) or [])
        return user_id in admin_ids
    except Exception:
        return False

def _mask_token(s: Optional[str], keep: int = 6) -> str:
    """Маскируем токен в логах."""
    if not isinstance(s, str):
        return str(s)
    return (s[:keep] + "…") if len(s) > keep else s

# ---------------- Клавиатуры ----------------
def _make_keyboard(is_workout: bool, user_id: int) -> ReplyKeyboardMarkup:
    rows = []
    if is_workout:
        # НОВОЕ: один снимок через 10–30 сек после нажатия
        rows.append([
            KeyboardButton(
                "▶️ Начать подход",
                web_app=WebAppInfo(
                    url=str(settings.WEBAPP_URL)
                    + "?mode=workout"
                    + "&shots=1"
                    + "&delay_min=10"
                    + "&delay_max=30"
                    + "&verify=home"
                )
            )
        ])
    rows.append([KeyboardButton("📊 Профиль")])

    # Админские кнопки
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
    return " ".join(RU_BY_EN[d] for d in days if d in RU_BY_EN)

# ---------------- Фото-проверка тренировки (опционально) ----------------
async def _save_training_photo(user_id: int, photo_bytes: bytes, bot) -> bool:
    """
    Сохраняет фото тренировки в sets и прогоняет GPT-проверку.
    Требования:
      — человек выполняет упражнение (не поза/селфи),
      — отсутствие признаков монтажа/скриншотов/старых фото,
      — ДОМ (квартира/комната/дом/домашний инвентарь), а не коммерческий зал.
    GPT должен вернуть JSON: {"success": bool, "is_home": bool, "reason": string}
    """
    from tempfile import NamedTemporaryFile
    from pathlib import Path

    logger.info("[sets] user=%s: received photo bytes=%d", user_id, len(photo_bytes))

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

        logger.info("[sets] user=%s: sending to GPT verify…", user_id)
        gpt = await verify_task_with_gpt(check_text, tmp_path)
        verified = bool(gpt.get("success"))
        is_home = bool(gpt.get("is_home"))
        reason = gpt.get("reason", "")

        if verified and not is_home:
            verified = False
            reason = reason or "Обстановка не похожа на домашнюю"

        logger.info("[sets] user=%s: GPT result verified=%s is_home=%s reason=%r",
                    user_id, verified, is_home, reason)

        async with Database.acquire() as conn:
            await conn.execute(
                "INSERT INTO sets (user_id, photo, verified, gpt_reason) VALUES ($1, $2, $3, $4)",
                user_id, photo_bytes, verified, reason
            )

        if verified:
            await bot.send_message(chat_id=user_id, text="✅ Фото засчитано (дом).")
        else:
            await bot.send_message(chat_id=user_id, text="❌ Не засчитано: " + (reason or "не прошла проверка"))
        return verified
    except Exception as e:
        logger.exception("Photo verify/save failed: %s", e)
        try:
            await bot.send_message(chat_id=user_id, text="⚠️ Не удалось проверить фото. Попробуй ещё раз.")
        except Exception:
            pass
        return False
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

# ---------------- Помощники сессии ----------------
def _set_session_active(context: ContextTypes.DEFAULT_TYPE, user_id: int, active: bool) -> None:
    sa = context.application.bot_data.setdefault("session_active", {})
    if active:
        sa[user_id] = True
    else:
        sa.pop(user_id, None)

def _is_session_active(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return bool(context.application.bot_data.get("session_active", {}).get(user_id))

# ---------------- Планировщик напоминаний ----------------
def _shift_days(days_tuple: tuple[int, ...], offset: int) -> tuple[int, ...]:
    """Сдвиг дней недели (0..6) на offset вперёд, с модулем 7."""
    return tuple(((d + offset) % 7) for d in days_tuple)
from zoneinfo import ZoneInfo
APP_TZ = ZoneInfo(getattr(settings, "TIMEZONE", "Europe/Moscow"))

def _schedule_reminders(context: ContextTypes.DEFAULT_TYPE, user_id: int, days: List[str], t: time, dur_min: int) -> None:
    jq = getattr(context.application, "job_queue", None)
    if jq is None:
        logger.warning("JobQueue is not available; skipping reminders for user %s", user_id)
        return

    # Сносим старые джобы пользователя
    try:
        for job in jq.jobs():
            if (job.name or "").startswith(f"{user_id}:"):
                job.schedule_removal()
    except Exception as e:
        logger.exception("Failed to list/remove jobs: %s", e)

    if not days:
        logger.info("[sched] user=%s: no days, skip scheduling", user_id)
        return

    # tz-aware время в APP_TZ
    t_z = time(t.hour, t.minute, t.second, t.microsecond, tzinfo=APP_TZ)
    base_dt = datetime.combine(datetime.now(APP_TZ).date(), t_z)
    mid_time = (base_dt + timedelta(minutes=max(dur_min // 2, 1))).timetz()
    end_time = (base_dt + timedelta(minutes=dur_min)).timetz()

    # PTB: ПН=0 ... ВС=6
    day_index = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
    valid_days_raw = tuple(day_index[d] for d in days if d in day_index)

    # ВАШ СДВИГ: «день раньше»
    valid_days = _shift_days(valid_days_raw, 1)

    # Если середина/конец уходят за полночь — переносим на след. день
    days_start = valid_days
    days_mid = valid_days if (mid_time > t_z) else _shift_days(valid_days, 1)
    days_end = valid_days if (end_time > t_z) else _shift_days(valid_days, 1)

    logger.info("[sched] user=%s: start=%s mid=%s end=%s tz=%s days_start=%s days_mid=%s days_end=%s dur=%s",
                user_id, t_z, mid_time, end_time, APP_TZ, days_start, days_mid, days_end, dur_min)

    async def start_cb(ctx: ContextTypes.DEFAULT_TYPE):
        _set_session_active(ctx, user_id, True)
        try:
            await ctx.bot.send_message(
                chat_id=user_id,
                text="🏁 Старт тренировки! Жми «▶️ Начать подход». Фото сделаю через 10–30 сек автоматически.",
                reply_markup=_make_keyboard(True, user_id)
            )
        except Exception as e:
            logger.exception("Failed to send START reminder to %s: %s", user_id, e)

    async def mid_cb(ctx: ContextTypes.DEFAULT_TYPE):
        _set_session_active(ctx, user_id, True)
        try:
            await ctx.bot.send_message(
                chat_id=user_id,
                text="⏳ Середина тренировки — контрольный подход. «▶️ Начать подход» (фото через 10–30 сек).",
                reply_markup=_make_keyboard(True, user_id)
            )
        except Exception as e:
            logger.exception("Failed to send MID reminder to %s: %s", user_id, e)

    async def end_cb(ctx: ContextTypes.DEFAULT_TYPE):
        _set_session_active(ctx, user_id, False)
        try:
            await ctx.bot.send_message(
                chat_id=user_id,
                text="✅ Конец тренировки — финальное подтверждение.",
                reply_markup=_make_keyboard(False, user_id)
            )
        except Exception as e:
            logger.exception("Failed to send END reminder to %s: %s", user_id, e)

    if not valid_days:
        logger.info("[sched] user=%s: valid_days empty, skip scheduling", user_id)
        return

    jq.run_daily(start_cb, time=t_z,      days=days_start, name=f"{user_id}:start")
    jq.run_daily(mid_cb,   time=mid_time, days=days_mid,   name=f"{user_id}:mid")
    jq.run_daily(end_cb,   time=end_time, days=days_end,   name=f"{user_id}:end")

    for job in jq.jobs():
        if (job.name or "").startswith(f"{user_id}:"):
            logger.info("[sched] %s next_run=%s", job.name, job.next_run_time)

# Подхват настроек из БД и переустановка джобов
async def _reschedule_from_db(update_or_context, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    try:
        async with Database.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT reminder_enabled, reminder_days, reminder_time, workout_duration
                  FROM users
                 WHERE user_id = $1
                """,
                user_id
            )
        if not row:
            logger.info("[sched] user=%s: no user row", user_id)
            return
        if not row["reminder_enabled"]:
            jq = getattr(context.application, "job_queue", None)
            if jq:
                for job in jq.jobs():
                    if (job.name or "").startswith(f"{user_id}:"):
                        job.schedule_removal()
            _set_session_active(context, user_id, False)
            logger.info("[sched] user=%s: reminders disabled, jobs removed", user_id)
            return

        days = list(row["reminder_days"] or [])
        rtime: Optional[time] = row["reminder_time"]
        dur = int(row["workout_duration"] or 60)
        logger.info("[sched] user=%s: reschedule days=%s time=%s dur=%s", user_id, days, rtime, dur)
        if rtime:
            _schedule_reminders(context, user_id, days, rtime, dur)
    except Exception as e:
        logger.exception("_reschedule_from_db failed for user %s: %s", user_id, e)

# ---------------- Хендлеры ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Регистрация = только мастер расписания. Камера не нужна."""
    message = update.message or update.callback_query.message
    user = update.effective_user

    async with Database.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, reminder_enabled FROM users WHERE user_id=$1",
            user.id
        )
        if not row:
            await conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (user_id) DO NOTHING
                """,
                user.id, user.username, user.first_name, user.last_name
            )
            row = {"reminder_enabled": False}
            logger.info("[start] user=%s: created user row", user.id)

    if not (row.get("reminder_enabled") if isinstance(row, dict) else row["reminder_enabled"]):
        context.user_data.clear()
        context.user_data["awaiting_reminder_days"] = True
        await message.reply_text(
            "🗓️ В какие дни тренируешься? Можно нажать кнопку или вписать:\n"
            "• «пн ср пт»  • «вт чт сб»  • «пн-пт»  • «каждый день»  • «сб вс»  • «без расписания»",
            reply_markup=_make_keyboard(False, user.id),
        )
        await message.reply_text("Выбери дни расписания:", reply_markup=days_keyboard())
        return

    await _reschedule_from_db(update, context, user.id)
    await message.reply_text(
        "Готово! Используй кнопки ниже.",
        reply_markup=_current_keyboard(context, user.id)
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Роутер мастера расписания + админ-кнопки."""
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

    # Админ: ручной старт/стоп режима
    if _is_admin(user.id):
        if low in ("🟢 старт тренировки (админ)", "старт тренировки", "🟢 старт тренировки", "/start_workout"):
            _set_session_active(context, user.id, True)
            logger.info("[admin] user=%s: manual START workout", user.id)
            await message.reply_text("🚀 Режим тренировки включён (админ). Жми «▶️ Начать подход». Фото сделаю через 10–30 сек.",
                                     reply_markup=_make_keyboard(True, user.id))
            return
        if low in ("🔴 стоп тренировки (админ)", "стоп тренировки", "🔴 стоп тренировки", "/end_workout"):
            _set_session_active(context, user.id, False)
            logger.info("[admin] user=%s: manual STOP workout", user.id)
            await message.reply_text("🛑 Режим тренировки выключен (админ).",
                                     reply_markup=_make_keyboard(False, user.id))
            return

    # Мастер: шаг дни
    if context.user_data.get("awaiting_reminder_days"):
        days = _parse_days(msg)
        logger.info("[wizard] user=%s: days parsed=%s", user.id, days)

        # без расписания — отключаем напоминания
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

    # Мастер: шаг время
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

    # Мастер: шаг длительность
    if context.user_data.get("awaiting_reminder_duration"):
        digits = re.findall(r"\d+", msg)
        if not digits:
            await message.reply_text(
                "Введи число минут (от 5 до 240), например: 5, 30, 95.",
                reply_markup=duration_keyboard()
            )
            return
        dur = int(digits[0])
        if not (5 <= dur <= 240):
            await message.reply_text(
                "Поддерживаю длительность от 5 до 240 минут. Введи число в этом диапазоне.",
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

        _schedule_reminders(context, user.id, days, t, dur)
        context.user_data.clear()

        await message.reply_text(
            f"✅ Напоминания включены.\n"
            f"Дни: {_human_days(days)}\n"
            f"Время: {t.strftime('%H:%M')}\n"
            f"Длительность: {dur} мин.",
            reply_markup=_make_keyboard(False, user.id)  # кнопка появится на старте или по админ-старту
        )
        return

    # Прочее
    if low in ("профиль", "📊 профиль"):
        await profile(update, context)
        return

    await message.reply_text("Не понял. Нажми кнопку ниже.",
                             reply_markup=_current_keyboard(context, user.id))

async def reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Явный перезапуск мастера настройки напоминаний."""
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
    """
    Поддерживаем:
      1) Финальный пакет:  {"type":"workout_set","tokens":["t1"], "timestamps":[...]}  # ОДНО фото (новый режим)
      2) Потоковые события: {"type":"single_photo_uploaded","token":"t1"} / {"type":"set_photo_uploaded","token":"t1"}
      3) (совместимость) три фото: {"type":"workout_set","tokens":["t1","t2","t3"], "window":180, "timestamps":[...]}
    """
    if not update.message or not update.message.web_app_data:
        return
    try:
        raw = update.message.web_app_data.data
        logger.info("[webapp] raw length=%s", len(raw) if raw is not None else None)
        payload = json.loads(raw)
    except Exception:
        logger.exception("[webapp] failed to parse web_app_data JSON")
        return

    ptype = str(payload.get("type"))

    # --- Потоковое одиночное событие: сразу проверяем 1 фото ---
    if ptype in ("single_photo_uploaded", "set_photo_uploaded"):
        user = update.effective_user
        token = payload.get("token") or payload.get("t") or payload.get("id")
        if not token:
            logger.warning("[webapp] user=%s %s without token", user.id, ptype)
            return
        logger.info("[webapp] user=%s single upload token=%s", user.id, _mask_token(token))

        # Скачиваем фото
        try:
            async with aiohttp.ClientSession() as sess:
                pull_url = settings.make_pull_url(token)
                async with sess.get(pull_url, timeout=30) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    photo_bytes = await r.read()
            logger.info("[webapp] user=%s single photo size=%d", user.id, len(photo_bytes))
        except Exception as e:
            logger.exception("[webapp] user=%s fail pull single photo token=%s: %s", user.id, _mask_token(token), e)
            await update.message.reply_text("⚠️ Не удалось получить фото. Попробуй ещё раз.")
            return

        # Проверка фото
        ok = await _save_training_photo(user.id, photo_bytes, context.bot)
        if ok:
            await update.message.reply_text("🏆 Подход засчитан (1 фото).")
        else:
            await update.message.reply_text("❌ Подход не засчитан (1 фото не прошло проверку).")
        return

    # --- Финальный пакет ---
    if ptype != "workout_set":
        logger.info("[webapp] skip payload type=%r", ptype)
        return

    user = update.effective_user
    tokens = payload.get("tokens") or payload.get("photos") or []
    window = int(payload.get("window") or 180)
    ts = payload.get("timestamps") or []

    logger.info("[webapp] user=%s type=workout_set window=%s tokens=%s ts_count=%s",
                user.id, window, [_mask_token(t) for t in tokens], (len(ts) if isinstance(ts, list) else 0))

    # Новый режим: одно фото внутри workout_set
    if len(tokens) == 1:
        token = tokens[0]
        # Скачиваем фото
        try:
            async with aiohttp.ClientSession() as sess:
                pull_url = settings.make_pull_url(token)
                async with sess.get(pull_url, timeout=30) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    photo_bytes = await r.read()
            logger.info("[webapp] user=%s single-in-set photo size=%d", user.id, len(photo_bytes))
        except Exception as e:
            logger.exception("[webapp] user=%s fail pull single-in-set token=%s: %s", user.id, _mask_token(token), e)
            await update.message.reply_text("⚠️ Не удалось получить фото. Попробуй ещё раз.")
            return

        ok = await _save_training_photo(user.id, photo_bytes, context.bot)
        if ok:
            await update.message.reply_text("🏆 Подход засчитан (1 фото).")
        else:
            await update.message.reply_text("❌ Подход не засчитан (1 фото не прошло проверку).")
        return

    # Совместимость: старый режим на 3 фото
    if len(tokens) != 3:
        await update.message.reply_text("⚠️ Ожидаю или 1 фото, или 3 фото.")
        logger.warning("[webapp] user=%s wrong tokens count=%s", user.id, len(tokens))
        return

    # 3 фото — старый путь
    photos_bytes: List[bytes] = []
    async with aiohttp.ClientSession() as sess:
        for idx, tok in enumerate(tokens, start=1):
            try:
                pull_url = settings.make_pull_url(tok)
                async with sess.get(pull_url, timeout=30) as r:
                    status = r.status
                    if status != 200:
                        logger.warning("[webapp] user=%s photo %d/%d token=%s HTTP %s",
                                       user.id, idx, len(tokens), _mask_token(tok), status)
                        raise RuntimeError(f"HTTP {status}")
                    data = await r.read()
                    photos_bytes.append(data)
                    logger.info("[webapp] user=%s photo %d/%d token=%s size=%d",
                                user.id, idx, len(tokens), _mask_token(tok), len(data))
            except Exception as e:
                logger.exception("[webapp] user=%s fail pull photo %d/%d token=%s: %s",
                                 user.id, idx, len(tokens), _mask_token(tok), e)
                await update.message.reply_text("⚠️ Не удалось получить фото. Попробуй ещё раз.")
                return

    logger.info("[webapp] user=%s all photos pulled count=%d", user.id, len(photos_bytes))

    # Валидация третей (если timestamps есть)
    thirds_ok = True
    if isinstance(ts, list) and len(ts) == 3:
        def _to_ts(x):
            try:
                if isinstance(x, (int, float)):
                    return float(x)
                return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
            except Exception:
                return None

        t_vals = list(map(_to_ts, ts))
        if any(v is None for v in t_vals):
            thirds_ok = False
            logger.info("[webapp] user=%s thirds check: timestamps parse failed %r", user.id, ts)
        else:
            t_vals.sort()
            total = t_vals[-1] - t_vals[0]
            if total <= 0:
                thirds_ok = False
                logger.info("[webapp] user=%s thirds check: non-positive total=%s", user.id, total)
            else:
                target_segment = window / 3.0
                tol = max(10.0, target_segment * 0.4)
                d1 = t_vals[1] - t_vals[0]
                d2 = t_vals[2] - t_vals[1]
                thirds_ok = (abs(d1 - target_segment) <= tol) and (abs(d2 - target_segment) <= tol)
                logger.info("[webapp] user=%s thirds d1=%.2f d2=%.2f target=%.2f tol=%.2f -> thirds_ok=%s",
                            user.id, d1, d2, target_segment, tol, thirds_ok)

    results = []
    for i, pb in enumerate(photos_bytes, start=1):
        ok = await _save_training_photo(user.id, pb, context.bot)
        results.append(ok)
        logger.info("[webapp] user=%s photo %d verify=%s", user.id, i, ok)

    logger.info("[webapp] user=%s set summary results=%s thirds_ok=%s", user.id, results, thirds_ok)

    if all(results) and thirds_ok:
        await update.message.reply_text("🏆 Подход засчитан: 3/3 фото, корректные трети и домашняя тренировка.")
    elif all(results) and not thirds_ok:
        await update.message.reply_text("✅ Фото ок, но интервалы третей не совпали с окном. Постарайся держать равные интервалы.")
    else:
        passed = sum(1 for x in results if x)
        tip = "" if thirds_ok else " и интервалы третей некорректны"
        await update.message.reply_text(f"❌ Подход не засчитан: {passed}/3 фото прошло{tip}.")

# ---------------- Админ-команды ----------------
async def delete_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.effective_message.reply_text("🚫 Доступ запрещён.", reply_markup=_current_keyboard(context, user.id))
        return
    try:
        await Database.drop()
        await Database.init()
        jq = getattr(context.application, "job_queue", None)
        if jq:
            for job in jq.jobs():
                job.schedule_removal()
        context.application.bot_data["session_active"] = {}
        await update.effective_message.reply_text("🗑️ База данных удалена и пересоздана.", reply_markup=_make_keyboard(False, user.id))
        logger.info("[admin] user=%s: /delete_db done", user.id)
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
                           workout_duration = NULL
                    """
                )
            except Exception:
                pass

        jq = getattr(context.application, "job_queue", None)
        if jq:
            for job in jq.jobs():
                job.schedule_removal()
        context.application.bot_data["session_active"] = {}

        await update.effective_message.reply_text("🧹 Данные очищены. Напоминания выключены у всех.", reply_markup=_make_keyboard(False, user.id))
        logger.info("[admin] user=%s: /clear_db done", user.id)
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
    logger.info("[admin] user=%s: /start_workout", user.id)
    await update.effective_message.reply_text("🚀 Режим тренировки включён (админ). Жми «▶️ Начать подход». Фото сделаю через 10–30 сек.",
                                              reply_markup=_make_keyboard(True, user.id))

async def end_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.effective_message.reply_text("🚫 Доступ запрещён.", reply_markup=_current_keyboard(context, user.id))
        return
    _set_session_active(context, user.id, False)
    logger.info("[admin] user=%s: /end_workout", user.id)
    await update.effective_message.reply_text("🛑 Режим тренировки выключен (админ).",
                                              reply_markup=_make_keyboard(False, user.id))

# ---------------- Профиль ----------------
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    user = update.effective_user

    total_tasks = 0
    completed_tasks = 0
    reminder_enabled = False
    days = []
    rtime: Optional[time] = None
    duration = None

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
                pass  # таблицы может не быть — ок

            row_user = await conn.fetchrow(
                """
                SELECT reminder_enabled, reminder_days, reminder_time, workout_duration
                  FROM users
                 WHERE user_id = $1
                """,
                user.id
            )
            if row_user:
                reminder_enabled = bool(row_user["reminder_enabled"])
                days = list(row_user["reminder_days"] or [])
                rtime = row_user["reminder_time"]
                duration = row_user["workout_duration"]

    except Exception as e:
        logger.exception("profile() failed: %s", e)

    percent = 0
    if total_tasks:
        percent = int((completed_tasks / total_tasks) * 100) if total_tasks else 0

    now_local = datetime.now(APP_TZ)
    tz_label = getattr(APP_TZ, "key", str(APP_TZ))  # Europe/Moscow
    today_line = now_local.strftime(f"Сегодня: %Y-%m-%d (%A) %H:%M ({tz_label})")

    text = [
        f"👤 Профиль @{user.username or user.id}",
        today_line,  # ← новая строка
        f"Задач: {total_tasks}, выполнено: {completed_tasks} ({percent}%)",
        "",
        "🔔 Напоминания: " + ("включены" if reminder_enabled else "выключены"),
        "Дни: " + _human_days(days),
        "Время: " + (rtime.strftime('%H:%M') if rtime else "—"),
        "Длительность: " + (f"{duration} мин." if duration else "—"),
        "",
        "Режим тренировки: " + ("активен" if _is_session_active(context, user.id) else "выключен"),
    ]
    await message.reply_text("\n".join(text), reply_markup=_current_keyboard(context, user.id))
