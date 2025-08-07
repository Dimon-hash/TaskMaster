import os
import pickle
import cv2
import json
import base64
import asyncio
import asyncpg
import logging
from pathlib import Path

from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Настройка логгирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# Конфигурация
class Config:
    POSTGRES = {
        'host': 'localhost',
        'port': '5432',
        'user': 'postgres',
        'password': 'Foscar',
        'database': 'fitness_bot'
    }

    OPENAI_API_KEY = "sk-aitunnel-omeCzGWxkHPU0cPnADKnWe481LTigfBf"
    OPENAI_BASE_URL = "https://api.aitunnel.ru/v1/"

    TELEGRAM_TOKEN = "8006388827:AAGg4xPDWHjQ8aaS30-fSy97YK7jBUUabgQ"

    TEMP_DIR = Path("temp")
    TEMP_DIR.mkdir(exist_ok=True)


# Инициализация клиентов
client = OpenAI(
    api_key=Config.OPENAI_API_KEY,
    base_url=Config.OPENAI_BASE_URL,
)

# --- Инициализация каскадного классификатора ---
def init_face_cascade():
    cascade_path = "haarcascade_frontalface_default.xml"

    if os.path.exists(cascade_path):
        classifier = cv2.CascadeClassifier(cascade_path)
        if not classifier.empty():
            return classifier

    try:
        opencv_path = os.path.join(cv2.data.haarcascades, cascade_path)
        if os.path.exists(opencv_path):
            classifier = cv2.CascadeClassifier(opencv_path)
            if not classifier.empty():
                return classifier
    except Exception:
        pass

    try:
        url = "https://raw.githubusercontent.com/opencv/opencv/master/data/haarcascades/haarcascade_frontalface_default.xml"
        urllib.request.urlretrieve(url, cascade_path)
        classifier = cv2.CascadeClassifier(cascade_path)
        if not classifier.empty():
            return classifier
    except Exception as e:
        print(f"Не удалось загрузить каскадный классификатор: {e}")

    return None


face_cascade = init_face_cascade()

# Утилиты для работы с БД
class Database:
    @staticmethod
    async def get_connection():
        return await asyncpg.connect(**Config.POSTGRES)

    @staticmethod
    async def init_db():
        conn = await Database.get_connection()
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    face_features BYTEA,
                    registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    task_text TEXT,
                    status TEXT,
                    completion_date TIMESTAMP,
                    verification_photo BYTEA
                )
            """)
        finally:
            await conn.close()


# Генерация заданий
class TaskGenerator:
    @staticmethod
    async def generate_gpt_task():
        prompt = """
        Придумай простое задание для спортзала, которое можно подтвердить фото. Условия:
        1. Используй ОДИН вид инвентаря (штанга, гантели, тренажер).
        2. Укажи только ОДНУ деталь (вес/количество/положение).
        3. Формат: "Сделай [действие] с [инвентарь] + [деталь]".
        4. Избегай сложных формулировок и заданий.
        5. Примеры хороших заданий:
           - "фото гантелей"
           - "Фото жима штанги"
        6. Задание должно быть в одно короткое предложение.
        7. Сделай понятное задание
        """
        try:
            response = client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Ошибка OpenAI: {e}")
            return "фото гантелей 12 кг в правой руке."


# Работа с изображениями
class ImageProcessor:
    @staticmethod

    def extract_face_features(image, face):
        (x, y, w, h) = face
        face_roi = image[y:y + h, x:x + w]

        # Улучшенная обработка изображения
        resized = cv2.resize(face_roi, (100, 100))
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        normalized = gray / 255.0  # Нормализация

        return normalized.flatten()

    @staticmethod
    async def extract_face_from_photo(image_path):
        try:
            image = cv2.imread(str(image_path))
            if image is None:
                raise ValueError("Не удалось загрузить изображение")

            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, 1.3, 5)

            if len(faces) == 0:
                return None

            return ImageProcessor.extract_face_features(image, faces[0])
        except Exception as e:
            logger.error(f"Ошибка обработки лица: {e}")
            return None

    @staticmethod
    def compare_faces(features1, features2, threshold=0.7):
        try:
            import numpy as np

            # Проверка входных данных
            if features1 is None or features2 is None:
                return False

            # Конвертируем в numpy массивы
            arr1 = np.array(features1, dtype=np.float32)
            arr2 = np.array(features2, dtype=np.float32)

            # Нормализация массивов
            arr1 = arr1 / np.linalg.norm(arr1)
            arr2 = arr2 / np.linalg.norm(arr2)

            # Вычисляем косинусное сходство (более надежно, чем MSE)
            similarity = np.dot(arr1, arr2) / (np.linalg.norm(arr1) * np.linalg.norm(arr2))

            # Сравниваем с порогом
            return similarity > threshold

        except Exception as e:
            logger.error(f"Ошибка сравнения лиц: {e}")
            return False


# Обработчики команд бота
class BotHandlers:
    @staticmethod
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        conn = await Database.get_connection()
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM users WHERE user_id = $1",
                user.id
            )

            if not exists:
                await update.message.reply_text(
                    "Добро пожаловать! Для регистрации отправьте селфи (фото вашего лица)."
                )
                context.user_data["awaiting_face"] = True
            else:
                await update.message.reply_text(
                    "Вы уже зарегистрированы! Используйте /gym_task для получения задания."
                )
        except Exception as e:
            logger.error(f"Ошибка в команде start: {e}")
            await update.message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")
        finally:
            await conn.close()

    @staticmethod
    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if context.user_data.get("awaiting_face"):
                await BotHandlers.handle_registration_photo(update, context)
            elif "current_task" in context.user_data:
                await BotHandlers.handle_task_photo(update, context)
            else:
                await update.message.reply_text(
                    "Я получил ваше фото, но не понимаю, что с ним делать.\n"
                    "Используйте /start для регистрации или /gym_task для получения задания."
                )
        except Exception as e:
            logger.error(f"Ошибка обработки фото: {e}")
            await update.message.reply_text("⚠️ Произошла ошибка при обработке фото. Попробуйте еще раз.")

    @staticmethod
    async def handle_registration_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        photo_file = await update.message.photo[-1].get_file()
        photo_path = Config.TEMP_DIR / f"face_{user.id}.jpg"

        try:
            await photo_file.download_to_drive(photo_path)

            # Проверка размера файла
            if photo_path.stat().st_size > 5 * 1024 * 1024:  # 5MB
                await update.message.reply_text("Фото слишком большое. Максимальный размер - 5MB.")
                return

            # Извлечение лица
            face_features = await ImageProcessor.extract_face_from_photo(photo_path)
            if face_features is None:
                await update.message.reply_text(
                    "❌ Лицо не обнаружено. Пожалуйста, попробуйте еще раз с соблюдением условий:\n"
                    "• Хорошее освещение\n"
                    "• Четкое изображение лица\n"
                    "• Без очков/шапок\n"
                    "• Лицо занимает большую часть фото"
                )
                return

            # Сохранение в БД
            conn = await Database.get_connection()
            try:
                await conn.execute(
                    """
                    INSERT INTO users 
                    (user_id, username, first_name, last_name, face_features) 
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    user.id, user.username, user.first_name, user.last_name,
                    pickle.dumps(face_features)
                )

                await update.message.reply_text(
                    "✅ Регистрация завершена!\n"
                    "Теперь вы можете получать задания командой /gym_task"
                )
                context.user_data["awaiting_face"] = False
            finally:
                await conn.close()

        except Exception as e:
            logger.error(f"Ошибка регистрации: {e}")
            await update.message.reply_text("⚠️ Произошла ошибка при регистрации. Пожалуйста, попробуйте позже.")
        finally:
            if photo_path.exists():
                photo_path.unlink()

    @staticmethod
    async def gym_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        conn = await Database.get_connection()
        try:
            # Проверка регистрации
            registered = await conn.fetchval(
                "SELECT 1 FROM users WHERE user_id = $1",
                user.id
            )

            if not registered:
                await update.message.reply_text(
                    "Вы не зарегистрированы. Отправьте /start для регистрации."
                )
                return

            # Генерация задания
            task = await TaskGenerator.generate_gpt_task()
            clean_task = task.replace("*", "").replace("_", "").replace("`", "")

            # Сохранение задания
            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (user_id, task_text, status)
                VALUES ($1, $2, 'issued')
                RETURNING task_id
                """,
                user.id, clean_task
            )

            context.user_data["current_task"] = clean_task
            context.user_data["current_task_id"] = task_id

            await update.message.reply_text(
                f"🎯 Ваше задание:\n\n{clean_task}\n\n"
                "Отправьте фото для проверки!"
            )
        except Exception as e:
            logger.error(f"Ошибка генерации задания: {e}")
            await update.message.reply_text("⚠️ Не удалось создать задание. Пожалуйста, попробуйте позже.")
        finally:
            await conn.close()

    @staticmethod
    async def handle_task_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        photo_file = await update.message.photo[-1].get_file()
        photo_path = Config.TEMP_DIR / f"task_{user.id}_{update.message.message_id}.jpg"

        try:
            await photo_file.download_to_drive(photo_path)
            await update.message.reply_text("🔄 Проверяю ваше задание...")

            task_text = context.user_data["current_task"]
            success, reason = await BotHandlers.check_task_completion(task_text, photo_path, user.id)

            # Сохранение результата
            conn = await Database.get_connection()
            try:
                with open(photo_path, 'rb') as f:
                    photo_data = f.read()

                await conn.execute(
                    """
                    UPDATE tasks 
                    SET status = $1, 
                        completion_date = CURRENT_TIMESTAMP, 
                        verification_photo = $2
                    WHERE task_id = $3
                    """,
                    "completed" if success else "failed",
                    photo_data,
                    context.user_data["current_task_id"]
                )

                if success:
                    await update.message.reply_text("✅ Задание выполнено и подтверждено!")
                else:
                    await update.message.reply_text(f"❌ Ошибка: {reason}")
            finally:
                await conn.close()

        except Exception as e:
            logger.error(f"Ошибка проверки задания: {e}")
            await update.message.reply_text("⚠️ Произошла ошибка при проверке задания. Попробуйте еще раз.")
        finally:
            if photo_path.exists():
                photo_path.unlink()

    @staticmethod
    async def check_task_completion(task_text: str, image_path: Path, user_id: int) -> (bool, str):
        try:
            # Проверка задания через GPT
            with open(image_path, "rb") as image_file:
                encoded_image = base64.b64encode(image_file.read()).decode('utf-8')

            system_prompt = """
            Ты - спортивный тренер. Проверь, соответствует ли фото заданию.
            Учитывай:
             1) наличие нужного инвентаря
             2) не учитывай количественные значения.
            Верни JSON: {"success": bool, "reason": string}
            """
            user_prompt = f"Задание: {task_text}\nФото соответствует?"

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{encoded_image}"
                                }
                            }
                        ]
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=300
            )

            result = json.loads(response.choices[0].message.content)
            if not result.get("success", False):
                return False, result.get("reason", "Задание не выполнено")

            # Проверка лица пользователя
            conn = await Database.get_connection()
            try:
                stored_features = await conn.fetchval(
                    "SELECT face_features FROM users WHERE user_id = $1",
                    user_id
                )

                if not stored_features:
                    return False, "Пользователь не зарегистрирован"

                current_features = await ImageProcessor.extract_face_from_photo(image_path)
                if not current_features:
                    return False, "Не удалось обнаружить лицо на фото"

                face_match = ImageProcessor.compare_faces(
                    pickle.loads(stored_features),
                    current_features
                )

                return face_match, "Задание выполнено" if face_match else "Фото не соответствует пользователю"
            finally:
                await conn.close()

        except Exception as e:
            logger.error(f"Ошибка проверки задания: {e}")
            return False, "Ошибка при анализе изображения"

    @staticmethod
    async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        conn = await Database.get_connection()
        try:
            stats = await conn.fetchrow(
                """
                SELECT 
                    COUNT(t.task_id) as total_tasks,
                    SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as completed_tasks,
                    u.registration_date
                FROM users u
                LEFT JOIN tasks t ON u.user_id = t.user_id
                WHERE u.user_id = $1
                GROUP BY u.user_id
                """,
                user.id
            )

            if stats:
                await update.message.reply_text(
                    f"👤 Профиль: {user.username or user.first_name}\n"
                    f"✅ Выполнено заданий: {stats['completed_tasks']}/{stats['total_tasks']}\n"
                    f"📅 Зарегистрирован: {stats['registration_date'].strftime('%d.%m.%Y')}"
                )
            else:
                await update.message.reply_text("Вы не зарегистрированы. Используйте /start")
        except Exception as e:
            logger.error(f"Ошибка профиля: {e}")
            await update.message.reply_text("⚠️ Произошла ошибка при загрузке профиля.")
        finally:
            await conn.close()


# Запуск бота
async def main():
    try:
        await Database.init_db()

        app = Application.builder().token(Config.TELEGRAM_TOKEN).build()

        # Регистрация обработчиков
        handlers = [
            CommandHandler("start", BotHandlers.start),
            CommandHandler("gym_task", BotHandlers.gym_task),
            CommandHandler("profile", BotHandlers.profile),
            MessageHandler(filters.PHOTO & ~filters.COMMAND, BotHandlers.handle_photo),
        ]

        for handler in handlers:
            app.add_handler(handler)

        logger.info("Бот запущен")
        await app.initialize()
        await app.start()
        await app.updater.start_polling()

        while True:
            await asyncio.sleep(3600)

    except asyncio.CancelledError:
        logger.info("Получен сигнал завершения...")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}", exc_info=True)
    finally:
        logger.info("Завершение работы бота...")
        if 'app' in locals():
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"Фатальная ошибка: {e}")
