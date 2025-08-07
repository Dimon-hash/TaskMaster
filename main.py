import os
import pickle
import cv2
import json
import base64

from openai import OpenAI
from PIL import Image

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)

import urllib.request

# Инициализация клиента OpenAI
client = OpenAI(
    api_key="sk-aitunnel-omeCzGWxkHPU0cPnADKnWe481LTigfBf",
    base_url="https://api.aitunnel.ru/v1/",
)



# --- Генератор заданий через GPT ---
async def generate_gpt_task():
    prompt = """
    Придумай простое задание для спортзала, которое можно подтвердить фото. Условия:
    1. Используй ОДИН вид инвентаря (штанга, гантели, тренажер).
    2. Укажи только ОДНУ деталь (вес/количество/положение).
    3. Формат: "Сделай [действие] с [инвентарь] + [деталь]".
    4. Избегай сложных формулировок и заданий.
    5. Примеры хороших заданий:
       - "фото гантелей 10 кг"
       - "Фото жима штанги 50 кг"
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
        print(f"Ошибка OpenAI: {e}")
        return "фото гантелей 12 кг в правой руке."


# --- Проверка выполнения задания через нейросеть ---
async def check_task_completion(task_text: str, image_path: str) -> (bool, str):
    """
    Проверяет выполнение задания с помощью мультимодальной модели GPT-4o
    Args:
        task_text: Текст задания
        image_path: Путь к изображению для проверки
    Returns:
        tuple: (success: bool, explanation: str)
    """
    try:
        # Кодируем изображение в base64
        with open(image_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode('utf-8')

        # Подготавливаем промпт для анализа
       ,
        # - Соответствие деталей (вес, количество)


        # Отправляем изображение и текст в мультимодальную модель
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

        # Парсим ответ
        result = json.loads(response.choices[0].message.content)
        return result.get("success", False), result.get("reason", "")

    except Exception as e:
        print(f"Ошибка при проверке задания: {e}")
        return False, "Ошибка при анализе изображения"

# --- Команда /gym_task ---
async def gym_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = await generate_gpt_task()
    clean_task = task.replace("*", "").replace("_", "").replace("`", "")

    try:
        await update.message.reply_text(
            f"🎯 *Ваше задание:*\n\n{clean_task}\n\n"
            "Отправьте фото/видео для проверки!",
            parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text(
            f"🎯 Ваше задание:\n\n{clean_task}\n\n"
            "Отправьте фото/видео для проверки!"
        )

    context.user_data["current_task"] = clean_task


# --- Обработка фото с заданием ---
async def handle_photo_with_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "current_task" not in context.user_data:
        await update.message.reply_text("Сначала получите задание через /gym_task")
        return

    # Сохраняем фото
    photo_file = await update.message.photo[-1].get_file()
    photo_path = f"temp_{update.message.message_id}.jpg"
    await photo_file.download_to_drive(photo_path)

    # Проверяем выполнение
    task_text = context.user_data["current_task"]
    success, reason = await check_task_completion(task_text, photo_path)

    # Отправляем результат
    if success:
        await update.message.reply_text(
            f"✅ Задание выполнено правильно!\n"
            f"Задание: {task_text}\n"
            f"Причина: {reason}"
        )
    else:
        await update.message.reply_text(
            f"❌ Задание не выполнено!\n"
            f"Задание: {task_text}\n"
            f"Причина: {reason}\n\n"
            f"Попробуйте еще раз!"
        )

    # Удаляем временный файл
    os.remove(photo_path)


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

# Настройки для распознавания лиц
FACE_DATA_DIR = "face_data"
os.makedirs(FACE_DATA_DIR, exist_ok=True)


# Функции для работы с лицами
def load_face_database():
    try:
        with open(os.path.join(FACE_DATA_DIR, "faces.pkl"), "rb") as f:
            return pickle.load(f)
    except (FileNotFoundError, EOFError):
        return {"features": [], "names": []}


def save_face_database(database):
    with open(os.path.join(FACE_DATA_DIR, "faces.pkl"), "wb") as f:
        pickle.dump(database, f)


def extract_face_features(image, face):
    (x, y, w, h) = face
    face_roi = image[y:y + h, x:x + w]
    return cv2.resize(face_roi, (100, 100)).flatten()


def compare_faces(features1, features2, threshold=0.7):
    mse = ((features1 - features2) ** 2).mean()
    return mse < threshold


def draw_faces(image_path, faces, names):
    image = cv2.imread(image_path)
    for (x, y, w, h), name in zip(faces, names):
        cv2.rectangle(image, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(image, name, (x, y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
    output_path = "output_" + os.path.basename(image_path)
    cv2.imwrite(output_path, image)
    return output_path


# Команды бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я могу:\n"
        "1. Давать задания для спортзала (/gym_task)\n"
        "2. Проверять их выполнение (отправь фото после получения задания)\n"
        "3. Распознавать лица (/nameface, /listfaces, /renameface)"
    )


async def list_faces(update: Update, context: ContextTypes.DEFAULT_TYPE):
    face_db = load_face_database()
    if not face_db["names"]:
        await update.message.reply_text("Нет сохраненных лиц.")
        return

    faces_list = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(face_db["names"]))
    await update.message.reply_text("Сохраненные лица:\n" + faces_list)


async def rename_face(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /renameface <номер> <новое_имя>")
        return

    try:
        face_num = int(context.args[0]) - 1
        new_name = ' '.join(context.args[1:])
        face_db = load_face_database()

        if face_num < 0 or face_num >= len(face_db["names"]):
            await update.message.reply_text("Неверный номер лица!")
            return

        old_name = face_db["names"][face_num]
        face_db["names"][face_num] = new_name
        save_face_database(face_db)

        await update.message.reply_text(f"Лицо успешно переименовано:\nБыло: {old_name}\nСтало: {new_name}")
    except ValueError:
        await update.message.reply_text("Номер должен быть целым числом!")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)}")


async def clear_faces(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Да, очистить", callback_data="clear_confirm")],
        [InlineKeyboardButton("Отмена", callback_data="clear_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Вы уверены, что хотите удалить ВСЕ сохранённые лица?", reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "clear_confirm":
        new_db = {"features": [], "names": []}
        save_face_database(new_db)
        await query.edit_message_text("✅ Все лица успешно удалены!")
    elif query.data == "clear_cancel":
        await query.edit_message_text("❌ Удаление отменено")


async def name_face(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /nameface <имя>")
        return

    face_db = load_face_database()
    if not face_db["features"]:
        await update.message.reply_text("Нет сохраненных лиц.")
        return

    new_name = ' '.join(context.args)
    face_db["names"][-1] = new_name
    save_face_database(face_db)
    await update.message.reply_text(f"Последнее лицо сохранено как: {new_name}")


if __name__ == "__main__":
    TOKEN = "8006388827:AAGg4xPDWHjQ8aaS30-fSy97YK7jBUUabgQ"
    try:
        app = Application.builder().token(TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("gym_task", gym_task))
        app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo_with_task))
        app.add_handler(CommandHandler("nameface", name_face))
        app.add_handler(CommandHandler("listfaces", list_faces))
        app.add_handler(CommandHandler("renameface", rename_face))
        app.add_handler(CommandHandler("clearfaces", clear_faces))
        app.add_handler(CallbackQueryHandler(button_handler))

        print("Бот запущен...")
        app.run_polling()
    except Exception as e:
        print(f"Ошибка запуска бота: {e}")
