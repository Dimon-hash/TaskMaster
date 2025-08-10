import logging
import pickle
from telegram import Update
from telegram.ext import ContextTypes
from database import Database
from image_processor import extract_face_from_photo, compare_faces
from gpt_tasks import generate_gpt_task, verify_task_with_gpt
from config import settings

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with (await Database.acquire()) as conn:
        exists = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", user.id)
    if not exists:
        await update.message.reply_text("Добро пожаловать! Отправьте селфи для регистрации.")
        context.user_data["awaiting_face"] = True
    else:
        await update.message.reply_text("Вы уже зарегистрированы. Используйте /gym_task")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_face"):
        return await handle_registration_photo(update, context)
    if context.user_data.get("current_task"):
        return await handle_task_photo(update, context)
    await update.message.reply_text("Неизвестный контекст. Используйте /start или /gym_task")


async def handle_registration_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото при регистрации"""
    user = update.effective_user
    photo_file = await update.message.photo[-1].get_file()
    path = settings.TEMP_DIR / f"face_{user.id}.jpg"
    await photo_file.download_to_drive(path)

    if path.stat().st_size > settings.MAX_PHOTO_SIZE:
        await update.message.reply_text("Фото слишком большое.")
        path.unlink(missing_ok=True)
        return

    features = await extract_face_from_photo(path)
    if features is None:
        await update.message.reply_text("Лицо не найдено. Попробуйте другое фото.")
        path.unlink(missing_ok=True)
        return

    with open(path, 'rb') as f:
        photo_bytes = f.read()

    async with (await Database.acquire()) as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, face_features, face_photo)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (user_id) DO UPDATE
            SET face_features = EXCLUDED.face_features,
                face_photo = EXCLUDED.face_photo
        """, user.id, user.username, user.first_name, user.last_name,
            pickle.dumps(features), photo_bytes
        )

    context.user_data["awaiting_face"] = False
    await update.message.reply_text("Регистрация завершена!")
    path.unlink(missing_ok=True)

async def handle_task_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото для проверки выполнения задания"""
    user = update.effective_user
    task_id = context.user_data.get("current_task_id")
    if not task_id:
        await update.message.reply_text("Нет активного задания.")
        return

    # Скачиваем фото пользователя
    photo_file = await update.message.photo[-1].get_file()
    path = settings.TEMP_DIR / f"task_{user.id}.jpg"
    await photo_file.download_to_drive(path)

    # Читаем файл в байты
    with open(path, 'rb') as f:
        photo_bytes = f.read()

    # 1️⃣ Извлекаем лицо с присланного фото
    features = await extract_face_from_photo(path)
    if features is None:
        await update.message.reply_text("Лицо не найдено. Попробуйте другое фото.")
        path.unlink(missing_ok=True)
        return

    # 2️⃣ Получаем сохранённые при регистрации фичи
    async with (await Database.acquire()) as conn:
        stored_features_bytes = await conn.fetchval(
            "SELECT face_features FROM users WHERE user_id = $1", user.id
        )

    if stored_features_bytes is None:
        await update.message.reply_text("Ваша регистрация повреждена — нет данных лица.")
        path.unlink(missing_ok=True)
        return

    stored_features = pickle.loads(stored_features_bytes)

    # 3️⃣ Сравниваем лица
    is_match_result = compare_faces(stored_features, features)

    if isinstance(is_match_result, (list, tuple)):
        is_match, similarity_score = is_match_result
    else:
        is_match, similarity_score = is_match_result, None

    await update.message.reply_text(f"Совпадение: {is_match}, коэффициент: {similarity_score}")

    if not is_match:
        await update.message.reply_text("❌ Лицо не совпадает с регистрационным фото.")
        path.unlink(missing_ok=True)
        return

    # 4️⃣ Проверяем выполнение задания через GPT
    task_text = context.user_data.get("current_task")
    gpt_result = await verify_task_with_gpt(task_text, str(path))
    # await update.message.reply_text(gpt_check)

    if not gpt_result.get("success", False):
        reason = gpt_result.get("reason", "Проверка не пройдена.")
        await update.message.reply_text(f"❌ GPT проверка не пройдена: {reason}")
        path.unlink(missing_ok=True)
        return

    # 5️⃣ Сохраняем результат в БД
    async with (await Database.acquire()) as conn:
        await conn.execute("""
            UPDATE tasks
            SET status = 'completed',
                completion_date = CURRENT_TIMESTAMP,
                verification_photo = $1
            WHERE task_id = $2
        """, photo_bytes, task_id)

    # 6️⃣ Очищаем контекст
    context.user_data["current_task"] = None
    context.user_data["current_task_id"] = None

    await update.message.reply_text("✅ Задание выполнено и проверено!")
    path.unlink(missing_ok=True)


async def gym_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with (await Database.acquire()) as conn:
        registered = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", user.id)
    if not registered:
        await update.message.reply_text("Вы не зарегистрированы. Используйте /start")
        return

    task = await generate_gpt_task()
    async with (await Database.acquire()) as conn:
        task_id = await conn.fetchval("""
            INSERT INTO tasks (user_id, task_text, status)
            VALUES ($1,$2,'issued')
            RETURNING task_id
        """, user.id, task)

    context.user_data["current_task"] = task
    context.user_data["current_task_id"] = task_id

    await update.message.reply_text(f"Задание: {task}\nОтправьте фото для проверки.")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with (await Database.acquire()) as conn:
        stats = await conn.fetchrow("""
            SELECT COUNT(t.task_id) AS total_tasks,
                   SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) AS completed_tasks,
                   u.registration_date
            FROM users u
            LEFT JOIN tasks t ON u.user_id = t.user_id
            WHERE u.user_id = $1
            GROUP BY u.user_id
        """, user.id)

    if not stats:
        await update.message.reply_text("Вы не зарегистрированы. Используйте /start")
        return

    total = stats['total_tasks'] or 0
    comp = stats['completed_tasks'] or 0
    percent = (comp / total * 100) if total else 0

    await update.message.reply_text(
        f"Выполнено: {comp}/{total} ({percent:.0f}%)\n"
        f"Зарегистрирован: {stats['registration_date'].strftime('%d.%m.%Y')}"
    )


async def send_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with (await Database.acquire()) as conn:
        photo_bytes = await conn.fetchval(
            "SELECT face_photo FROM users WHERE user_id = $1", user.id
        )

    if not photo_bytes:
        await update.message.reply_text("Фото не найдено. Вы ещё не регистрировались.")
        return

    await update.message.reply_photo(
        photo=photo_bytes,
        caption="Ваше сохранённое фото для идентификации"
    )
