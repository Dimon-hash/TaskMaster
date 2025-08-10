import logging
import pickle
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from database import Database
from image_processor import extract_face_from_photo, compare_faces
from gpt_tasks import generate_gpt_task, verify_task_with_gpt
from config import settings
from pathlib import Path

logger = logging.getLogger(__name__)

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🏋️ Получить задание"), KeyboardButton("📊 Профиль")]],
        resize_keyboard=True,
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with Database.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT training_program FROM users WHERE user_id = $1",
            user.id
        )
    if not user_row:
        await update.message.reply_text("👋 Добро пожаловать! Отправьте селфи 📸 для регистрации.")
        context.user_data["awaiting_face"] = True
    elif not user_row["training_program"]:
        await update.message.reply_text("✍️ Расскажите о своей программе тренировок или целях.")
        context.user_data["awaiting_program"] = True
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

    context.user_data["awaiting_face"] = False
    context.user_data["awaiting_program"] = True
    await update.message.reply_text(
        "📋 Опишите вашу программу тренировок или цели. Это поможет подбирать задания.",
        reply_markup=main_keyboard()
    )

    path.unlink(missing_ok=True)

async def handle_task_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото для проверки выполнения задания"""
    user = update.effective_user
    task_id = context.user_data.get("current_task_id")
    task_text = context.user_data.get("current_task")

    if not task_id or not task_text:
        await update.message.reply_text("⚠️ Нет активного задания.")
        return

    photo_file = await update.message.photo[-1].get_file()
    path = settings.TEMP_DIR / f"task_{user.id}.jpg"
    await photo_file.download_to_drive(path)

    try:
        with open(path, 'rb') as f:
            photo_bytes = f.read()

        # 1) извлекаем фичи
        features = await extract_face_from_photo(path)
        if features is None:
            await update.message.reply_text("😕 Лицо не найдено. Попробуйте другое фото.")
            return

        # 2) сравниваем с эталоном из БД
        async with Database.acquire() as conn:
            ref_row = await conn.fetchrow(
                "SELECT face_features FROM users WHERE user_id = $1",
                user.id
            )

        if ref_row and ref_row["face_features"]:
            try:
                stored_features = pickle.loads(ref_row["face_features"])
                match, score = compare_faces(stored_features, features)
                if not match:
                    await update.message.reply_text("🚫 Лицо не совпало с профилем. Пришлите другое фото.")
                    return
            except Exception as e:
                logger.exception("Ошибка сравнения лиц: %s", e)

        # 3) GPT-проверка (путь должен существовать до вызова)
        gpt_result = await verify_task_with_gpt(task_text, str(path))
        if not gpt_result.get("success", False):
            reason = gpt_result.get("reason", "Проверка не пройдена.")
            await update.message.reply_text(f"❌ GPT проверка не пройдена: {reason}")
            return

        # 4) апдейт задачи
        async with Database.acquire() as conn:
            await conn.execute(
                """
                UPDATE tasks
                SET status = 'completed',
                    completion_date = CURRENT_TIMESTAMP,
                    verification_photo = $1
                WHERE task_id = $2
                """,
                photo_bytes, task_id
            )

        # 5) чистим состояние
        context.user_data["current_task"] = None
        context.user_data["current_task_id"] = None

        await update.message.reply_text("✅ Задание выполнено и проверено! 🏆", reply_markup=main_keyboard())

    finally:
        # удаляем временный файл в конце
        Path(path).unlink(missing_ok=True)

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
        f"📋 Задание: {task}\n📸 Отправьте фото для проверки.",
        reply_markup=main_keyboard(),
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    user = update.effective_user
    async with Database.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT COUNT(t.task_id) AS total_tasks,
                   SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) AS completed_tasks,
                   u.registration_date
            FROM users u
            LEFT JOIN tasks t ON u.user_id = t.user_id
            WHERE u.user_id = $1
            GROUP BY u.user_id
            """,
            user.id
        )

    if not stats:
        await message.reply_text("🚫 Вы не зарегистрированы. Используйте /start")
        return

    total = stats['total_tasks'] or 0
    comp = stats['completed_tasks'] or 0
    percent = (comp / total * 100) if total else 0

    await message.reply_text(
        f"📊 Выполнено: {comp}/{total} ({percent:.0f}%)\n"
        f"🗓️ Зарегистрирован: {stats['registration_date'].strftime('%d.%m.%Y')}",
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

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_program"):
        program = update.message.text.strip()
        async with Database.acquire() as conn:
            await conn.execute(
                "UPDATE users SET training_program=$1 WHERE user_id=$2",
                program, update.effective_user.id,
            )
        context.user_data["awaiting_program"] = False
        await update.message.reply_text("✅ Программа сохранена!", reply_markup=main_keyboard())
        return

    await update.message.reply_text("⚠️ Неизвестный текст. Используйте меню или команды.")
