# handlers.py
import logging
import pickle
import json
import re
from datetime import datetime, timedelta, time
from pathlib import Path
import pytz
import aiohttp

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from telegram.ext import ContextTypes

from database import Database
from image_processor import extract_face_from_photo, compare_faces
from gpt_tasks import generate_gpt_task, verify_task_with_gpt
from config import settings

logger = logging.getLogger(__name__)

# ---------------- Константы/настройки ----------------
WEEKDAYS_MAP = {
    'пн': 'mon', 'пон': 'mon', 'понедельник': 'mon',
    'вт': 'tue', 'вторник': 'tue',
    'ср': 'wed', 'среда': 'wed',
    'чт': 'thu', 'четверг': 'thu',
    'пт': 'fri', 'пятница': 'fri',
    'сб': 'sat', 'суббота': 'sat',
    'вс': 'sun', 'воскресенье': 'sun'
}

# Можно переопределить таймзону в config.settings.TIMEZONE
TZ = pytz.timezone(getattr(settings, "TIMEZONE", "Europe/Moscow"))


# ---------------- Утилиты ----------------
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📸 Сделать фото", web_app=WebAppInfo(url=str(settings.WEBAPP_URL)))],
            [KeyboardButton("📊 Профиль")],
        ],
        resize_keyboard=True,
    )
def registration_form_text() -> str:
    return (
        "✍️ Расскажите о своей программе тренировок и целях\n"
        "Чтобы я выдавал задания ровно под вас (и чтобы их можно было подтвердить одним фото), "
        "ответьте коротко по пунктам. Можно списком, без романов.\n\n"
        "1) Цели на 1–2 месяца (несколько можно)\n"
        "Похудение / Набор мышц / Сила / Выносливость / Бокс / Осанка/спина/шея / Другое: ___\n\n"
        "2) Опыт и ограничения\n"
        "Уровень: новичок / средний / продвинутый\n"
        "Травмы/боль: ___\n"
        "Что нельзя/не хочу: ___\n\n"
        "3) Доступный инвентарь (зал/дом, что есть?)\n\n"
        "4) Режим: сколько раз в неделю; длительность: 20–30 / 40–60 / >60; "
        "плавный режим без жёсткого расписания: да/нет\n\n"
        "5) Предпочтения по упражнениям (штанга/тренажёры/турник/кардио/бокс/шея/кор/другое)\n\n"
        "6) Чего НЕ надо предлагать: ___\n\n"
        "7) Фото-подтверждение: лицо ок? да/нет; удобнее: селфи у снаряда / фото снаряда; "
        "можно фото блинов/гири с весом? да/нет\n\n"
        "8) Метрики прогресса: вес/повторы/время/дистанция/пульс/RPE? Что важнее лично вам?\n\n"
        "9) Мини-челленджи иногда нужны? да/нет\n\n"
        "10) Комментарии: ___\n\n"
        "После анкеты спрошу дни/время для напоминаний 💡"
    )

def _parse_time_hhmm(s: str) -> time | None:
    m = re.search(r'(\d{1,2})[:.](\d{2})', s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh < 24 and 0 <= mm < 60:
        return time(hour=hh, minute=mm)
    return None

def _parse_days(s: str) -> list[str]:
    s = s.strip().lower()
    if 'каждый день' in s or 'ежеднев' in s or 'все дни' in s or 'пн-вс' in s:
        return ['mon','tue','wed','thu','fri','sat','sun']
    rng = re.search(r'(пн|пон|вт|ср|чт|пт|сб|вс)\s*-\s*(пн|пон|вт|ср|чт|пт|сб|вс)', s)
    if rng:
        a, b = WEEKDAYS_MAP[rng.group(1)], WEEKDAYS_MAP[rng.group(2)]
        order = ['mon','tue','wed','thu','fri','sat','sun']
        ia, ib = order.index(a), order.index(b)
        return order[ia:ib+1] if ia <= ib else order[ia:]+order[:ib+1]
    days = []
    for token in re.split(r'[,\s]+', s):
        if token in WEEKDAYS_MAP:
            days.append(WEEKDAYS_MAP[token])
    return list(dict.fromkeys(days))

def _dur_to_minutes(d: str) -> int:
    d = d.strip().replace('мин', '').replace(' ', '').replace('—', '-')
    if '20-30' in d:
        return 30
    if '40-60' in d:
        return 60
    return 75  # >60


# ---------------- Твои хендлеры (с расширениями) ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with Database.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT training_program, training_form FROM users WHERE user_id = $1",
            user.id
        )
    if not user_row:
        await update.message.reply_text("👋 Добро пожаловать! Отправьте селфи 📸 для регистрации.")
        context.user_data["awaiting_face"] = True
        return
    elif not user_row["training_program"]:
        await update.message.reply_text("✍️ Расскажите о своей программе тренировок или целях.")
        context.user_data["awaiting_program"] = True
        return
    elif not user_row["training_form"]:
        context.user_data["awaiting_form"] = True
        await update.message.reply_text(registration_form_text())
        return
    else:
        await update.message.reply_text(
            "ℹ️ Вы уже зарегистрированы. Используйте меню ниже 💪",
            reply_markup=main_keyboard(),
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_face"):
        return await handle_registration_photo(update, context)
    if context.user_data.get("current_task"):
        return await handle_task_photo(update, context)
    await update.message.reply_text("⚠️ Неизвестный контекст. Используйте /start или /gym_task")

async def handle_registration_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото при регистрации"""
    user = update.effective_user
    photo_file = await update.message.photo[-1].get_file()
    path = settings.TEMP_DIR / f"face_{user.id}.jpg"
    await photo_file.download_to_drive(path)

    if path.stat().st_size > settings.MAX_PHOTO_SIZE:
        await update.message.reply_text("⚠️ Фото слишком большое.")
        path.unlink(missing_ok=True)
        return

    features = await extract_face_from_photo(path)
    if features is None:
        await update.message.reply_text("😕 Лицо не найдено. Попробуйте другое фото.")
        path.unlink(missing_ok=True)
        return

    with open(path, 'rb') as f:
        photo_bytes = f.read()

    async with Database.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, face_features, face_photo)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (user_id) DO UPDATE
            SET face_features = EXCLUDED.face_features,
                face_photo = EXCLUDED.face_photo
            """,
            user.id, user.username, user.first_name, user.last_name,
            pickle.dumps(features), photo_bytes
        )

    await update.message.reply_photo(photo=photo_bytes, caption="✅ Лицо сохранено для идентификации")

    # --- ДОБАВЛЕНО: просим и программу, и анкету ---
    context.user_data["awaiting_face"] = False
    context.user_data["awaiting_program"] = True
    await update.message.reply_text(
        "📋 Опишите вашу программу тренировок или цели. Это поможет подбирать задания.",
        reply_markup=main_keyboard()
    )
    context.user_data["awaiting_form"] = True
    await update.message.reply_text(registration_form_text())

    path.unlink(missing_ok=True)


async def _process_photo_bytes(user_id: int, photo_bytes: bytes, task_id: int | None, task_text: str | None, bot) -> bool:
    """Возвращает True, если верификация прошла и задача закрыта."""
    from tempfile import NamedTemporaryFile
    with NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(photo_bytes)
        tmp.flush()
        tmp_path = tmp.name
    try:
        features = await extract_face_from_photo(Path(tmp_path))
        if features is None:
            await bot.send_message(chat_id=user_id, text="😕 Лицо не найдено. Попробуйте другое фото.")
            return False

        # 2) сверка с эталоном
        async with Database.acquire() as conn:
            ref_row = await conn.fetchrow("SELECT face_features FROM users WHERE user_id=$1", user_id)
        if ref_row and ref_row["face_features"]:
            try:
                stored_features = pickle.loads(ref_row["face_features"])
                match, _ = compare_faces(stored_features, features)
                if not match:
                    await bot.send_message(chat_id=user_id, text="🚫 Лицо не совпало с профилем. Пришлите другое фото.")
                    return False
            except Exception as e:
                logger.exception("Ошибка сравнения лиц: %s", e)

        # 3) GPT‑проверка (если есть задание)
        if task_text:
            gpt_result = await verify_task_with_gpt(task_text, tmp_path)
            if not gpt_result.get("success", False):
                reason = gpt_result.get("reason", "Проверка не пройдена.")
                await bot.send_message(chat_id=user_id, text=f"❌ GPT проверка не пройдена: {reason}")
                return False

        # 4) апдейт задачи (если была)
        if task_id:
            async with Database.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE tasks
                    SET status='completed', completion_date=CURRENT_TIMESTAMP, verification_photo=$1
                    WHERE task_id=$2
                    """,
                    photo_bytes, task_id
                )
        await bot.send_message(chat_id=user_id, text="✅ Фото принято, проверка пройдена! 🏆", reply_markup=main_keyboard())
        return True
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

async def handle_task_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    task_id = context.user_data.get("current_task_id")
    task_text = context.user_data.get("current_task")
    if not task_id or not task_text:
        await update.message.reply_text("⚠️ Нет активного задания.")
        return
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    await _process_photo_bytes(user.id, bytes(photo_bytes), task_id, task_text, context.bot)


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.web_app_data:
        return
    try:
        payload = json.loads(update.message.web_app_data.data)
    except Exception:
        return
    if payload.get("type") != "photo_uploaded":
        return
    token = payload.get("token")
    if not token:
        return

    user_id = update.effective_user.id

    # тянем файл с вашего сервера по токену
    pull_url = f"{settings.WEBAPP_API_PULL_URL}?token={token}"
    async with aiohttp.ClientSession() as sess:
        async with sess.get(pull_url, timeout=30) as r:
            if r.status != 200:
                await update.message.reply_text("⚠️ Не удалось получить фото с сервера.")
                return
            photo_bytes = await r.read()

    # берём активное задание (если есть) из БД или из user_data
    async with Database.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT task_id, task_text FROM tasks
            WHERE user_id=$1 AND status='issued'
            ORDER BY task_id DESC LIMIT 1
            """,
            user_id
        )
    task_id = row["task_id"] if row else context.user_data.get("current_task_id")
    task_text = row["task_text"] if row else context.user_data.get("current_task")

    await _process_photo_bytes(user_id, photo_bytes, task_id, task_text, context.bot)

async def gym_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    user = update.effective_user

    async with Database.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT training_program FROM users WHERE user_id = $1",
            user.id
        )

    if not user_row:
        await message.reply_text("🚫 Вы не зарегистрированы. Используйте /start")
        return

    training_program = user_row["training_program"]
    if not training_program:
        await message.reply_text("✍️ Сначала расскажите о своей программе тренировок. Отправьте текст.")
        context.user_data["awaiting_program"] = True
        return

    # фикс: передаём программу в функцию
    task = await generate_gpt_task(training_program)

    async with Database.acquire() as conn:
        task_id = await conn.fetchval(
            """
            INSERT INTO tasks (user_id, task_text, status)
            VALUES ($1,$2,'issued')
            RETURNING task_id
            """,
            user.id, task
        )

    context.user_data["current_task"] = task
    context.user_data["current_task_id"] = task_id

    await message.reply_text(
        f"📋 Задание: {task}\n\nНажми ‘📸 Сделать фото’ внизу, я проверю автоматически.",
        reply_markup=main_keyboard(),
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    user = update.effective_user

    async with Database.acquire() as conn:
        urow = await conn.fetchrow(
            """
            SELECT face_photo, registration_date, training_program, training_form,
                   reminder_enabled, reminder_days, reminder_time, reminder_duration
            FROM users WHERE user_id = $1
            """,
            user.id
        )

        if not urow:
            await message.reply_text("🚫 Вы не зарегистрированы. Используйте /start")
            return

        trow = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_tasks,
                COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) AS completed_tasks
            FROM tasks
            WHERE user_id = $1
            """,
            user.id
        )

    total = int(trow["total_tasks"] or 0)
    comp = int(trow["completed_tasks"] or 0)
    percent = (comp / total * 100) if total else 0

    # Формируем блок анкеты
    training_form_str = ""
    if urow["training_form"]:
        try:
            form_data = json.loads(urow["training_form"])
            training_form_str = form_data.get("raw", "")
        except Exception:
            training_form_str = str(urow["training_form"])

    reminders_str = "❌ Выключены"
    if urow["reminder_enabled"]:
        days_str = " ".join(urow["reminder_days"] or [])
        time_str = urow["reminder_time"].strftime("%H:%M") if urow["reminder_time"] else "—"
        dur_str = f"{urow['reminder_duration']} мин"
        reminders_str = f"✅ {days_str} в {time_str}, длительность {dur_str}"

    caption = (
        f"📊 Выполнено: {comp}/{total} ({percent:.0f}%)\n"
        f"🗓️ Зарегистрирован: {urow['registration_date'].strftime('%d.%m.%Y') if urow.get('registration_date') else '—'}\n\n"
        # f"🏋️ Задания:\n{urow['training_program'] or '—'}\n\n"
        f"📋 Анкета:\n{training_form_str or '—'}\n\n"
        f"⏰ Напоминания: {reminders_str}"
    )

    photo_bytes = urow.get("face_photo")
    if photo_bytes:
        await message.reply_photo(
            photo=photo_bytes,
            caption=caption,
            reply_markup=main_keyboard(),
        )
    else:
        await message.reply_text(
            caption,
            reply_markup=main_keyboard(),
        )


async def delete_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != settings.ADMIN_ID:
        await update.message.reply_text("🚫 Доступ запрещён.")
        return
    await Database.drop()
    await Database.init()
    await update.message.reply_text("🗑️ База данных удалена.")

async def send_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with Database.acquire() as conn:
        photo_bytes = await conn.fetchval(
            "SELECT face_photo FROM users WHERE user_id = $1", user.id
        )

    if not photo_bytes:
        await update.message.reply_text("⚠️ Фото не найдено. Вы ещё не регистрировались.")
        return

    await update.message.reply_photo(
        photo=photo_bytes,
        caption="Ваше сохранённое фото для идентификации"
    )

# --------- НОВОЕ: обработка анкеты и напоминаний ---------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) Сохранение твоей программы (оставлено как было)
    if context.user_data.get("awaiting_program"):
        program = update.message.text.strip()
        async with Database.acquire() as conn:
            await conn.execute(
                "UPDATE users SET training_program=$1 WHERE user_id=$2",
                program, update.effective_user.id,
            )
        context.user_data["awaiting_program"] = False
        await update.message.reply_text("✅ Программа сохранена!", reply_markup=main_keyboard())
        # не выходим — пользователь может сразу прислать и анкету

    text = (update.message.text or "").strip()

    # 2) Анкета (новое)
    if context.user_data.get("awaiting_form"):
        async with Database.acquire() as conn:
            await conn.execute(
                "UPDATE users SET training_form=$1 WHERE user_id=$2",
                json.dumps({"raw": text}, ensure_ascii=False),
                update.effective_user.id
            )
        context.user_data["awaiting_form"] = False
        context.user_data["awaiting_reminder_days"] = True
        await update.message.reply_text(
            "🗓️ В какие дни обычно тренируешься? Примеры: «пн ср пт», «пн-пт», «каждый день»."
        )
        return

    # 3) Дни для напоминаний
    if context.user_data.get("awaiting_reminder_days"):
        days = _parse_days(text)
        if not days:
            await update.message.reply_text("Не распознал дни. Пример: «пн ср пт», «пн-пт», «каждый день».")
            return
        context.user_data["reminder_days"] = days
        context.user_data["awaiting_reminder_days"] = False
        context.user_data["awaiting_reminder_time"] = True
        await update.message.reply_text("⏰ Во сколько обычно начинаешь тренировку? (например, 19:30)")
        return

    # 4) Время
    if context.user_data.get("awaiting_reminder_time"):
        t = _parse_time_hhmm(text)
        if not t:
            await update.message.reply_text("Не понял время. Пример: 19:30")
            return
        context.user_data["reminder_time"] = t
        context.user_data["awaiting_reminder_time"] = False
        context.user_data["awaiting_reminder_duration"] = True
        await update.message.reply_text("⏱️ Введите длительность тренировки в минутах, например: 45")
        return

    # 5) Длительность + включаем напоминания
    if context.user_data.get("awaiting_reminder_duration"):
        try:
            dur_min = int(re.sub(r'\D', '', text))  # вытащим только числа
            if dur_min <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите длительность тренировки в минутах, например: 45")
            return

        user_id = update.effective_user.id
        days = context.user_data["reminder_days"]
        t: time = context.user_data["reminder_time"]

        async with Database.acquire() as conn:
            await conn.execute(
                """
                UPDATE users
                SET reminder_enabled = TRUE,
                    reminder_days = $1,
                    reminder_time = $2,
                    reminder_duration = $3
                WHERE user_id = $4
                """,
                days, t, str(dur_min), user_id
            )

        _schedule_reminders(context, user_id, days, t, dur_min)
        context.user_data["awaiting_reminder_duration"] = False
        await update.message.reply_text(
            f"✅ Анкета сохранена. Напоминания включены на {dur_min} минут.",
            reply_markup=main_keyboard()
        )
        return



    # 6) Всё прочее
    await update.message.reply_text("⚠️ Неизвестный текст. Используйте меню или команды.")

# --------- Планировщик напоминаний ---------
def _schedule_reminders(context: ContextTypes.DEFAULT_TYPE, user_id: int, days: list[str], t: time, dur: int):
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

    mid_time = (datetime.combine(datetime.now().date(), t) + timedelta(minutes=dur // 2)).time()
    end_time = (datetime.combine(datetime.now().date(), t) + timedelta(minutes=dur)).time()

    async def _create_new_task_and_prompt(ctx: ContextTypes.DEFAULT_TYPE, phase_text: str):
        """
        ВСЕГДА создаёт новое задание (даже если было старое),
        сохраняет в БД и ставит как активное, затем просит фото.
        """
        try:
            # 1) тянем программу
            async with Database.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT training_program FROM users WHERE user_id = $1",
                    user_id
                )

            if not row or not row["training_program"]:
                await ctx.bot.send_message(
                    chat_id=user_id,
                    text=f"{phase_text}\n(нет сохранённой программы — отправь её текстом командой /start)"
                )
                return

            training_program = row["training_program"]

            # 2) генерим новое задание
            task_text = await generate_gpt_task(training_program)

            # 3) пишем в БД
            async with Database.acquire() as conn:
                task_id = await conn.fetchval(
                    """
                    INSERT INTO tasks (user_id, task_text, status)
                    VALUES ($1,$2,'issued')
                    RETURNING task_id
                    """,
                    user_id, task_text
                )

            # 4) делаем его активным (перезаписываем старое)
            ud = ctx.application.bot_data.setdefault("user_tasks", {})
            ud[user_id] = {
                "current_task": task_text,
                "current_task_id": task_id
            }

            # 5) отправляем пользователю
            await ctx.bot.send_message(
                chat_id=user_id,
                text=(f"{phase_text}\n\n"
                      f"📋 Новое задание: {task_text}\n\n"
                      "Нажми ‘📸 Сделать фото’ — камера откроется и через 3 сек. я сделаю кадр автоматически.")
            )
        except Exception as e:
            logger.exception("_create_new_task_and_prompt failed for user %s: %s", user_id, e)
            try:
                await ctx.bot.send_message(chat_id=user_id, text=f"{phase_text}\n(ошибка генерации задания)")
            except Exception:
                pass

    async def start_cb(ctx: ContextTypes.DEFAULT_TYPE):
        await _create_new_task_and_prompt(ctx, "🏁 Старт тренировки!")

    async def mid_cb(ctx: ContextTypes.DEFAULT_TYPE):
        await _create_new_task_and_prompt(ctx, "⏳ Середина тренировки — контрольное задание.")

    async def end_cb(ctx: ContextTypes.DEFAULT_TYPE):
        await _create_new_task_and_prompt(ctx, "✅ Конец тренировки — финальное задание.")

    day_index = {'mon':0,'tue':1,'wed':2,'thu':3,'fri':4,'sat':5,'sun':6}
    for d in days:
        if d not in day_index:
            continue
        wd = day_index[d]
        jq.run_daily(start_cb, time=t,        days=(wd,), name=f"{user_id}:start:{d}")
        jq.run_daily(mid_cb,   time=mid_time, days=(wd,), name=f"{user_id}:mid:{d}")
        jq.run_daily(end_cb,   time=end_time, days=(wd,), name=f"{user_id}:end:{d}")

    logger.info("Scheduled reminders+new-tasks for user=%s days=%s at=%s dur=%s min", user_id, days, t, dur)


# --------- По желанию: команда заново настроить напоминания ---------
async def setup_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_reminder_days"] = True
    await update.message.reply_text(
        "🗓️ Обновим расписание. В какие дни тренируешься? (пн ср пт / пн-пт / каждый день)"
    )
