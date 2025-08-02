import os
import pickle
import cv2
from openai import OpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)
from ultralytics import YOLO
import urllib.request

os.environ["OPENAI_API_KEY"] = "sk-aitunnel-omeCzGWxkHPU0cPnADKnWe481LTigfBf"
client = OpenAI()

# --- Генератор заданий через GPT ---
async def generate_gpt_task():
    prompt = """
    Придумай оригинальное задание для спортзала, которое нужно подтвердить фото/видео. Условия:
    1. Используй конкретный инвентарь (штанга, гантели, тренажеры).
    2. Укажи детали: вес, ракурс, действие (например, "селфи с гантелей 12 кг в левой руке").
    3. Задание должно быть в одно предложение.
    4. Избегай шаблонов. Будь креативным!
    """

    try:
        response = client.chat.completions.create(  # Новый синтаксис
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print(f"Ошибка OpenAI: {e}")
        return "Селфи с гантелей 12 кг в правой руке."


# --- Команда /gym_task ---
async def gym_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = await generate_gpt_task()
    await update.message.reply_text(
        f"🎯 *Ваше задание:*\n\n{task}\n\n"
        "Отправьте фото/видео для проверки!",
        parse_mode="Markdown"
    )
    context.user_data["current_task"] = task  # Сохраняем задание

# --- Инициализация каскадного классификатора ---
def init_face_cascade():
    # Путь для сохранения каскада
    cascade_path = "haarcascade_frontalface_default.xml"

    # Если файл уже существует
    if os.path.exists(cascade_path):
        classifier = cv2.CascadeClassifier(cascade_path)
        if not classifier.empty():
            return classifier

    # Пробуем найти в пакете opencv
    try:
        opencv_path = os.path.join(cv2.data.haarcascades, cascade_path)
        if os.path.exists(opencv_path):
            classifier = cv2.CascadeClassifier(opencv_path)
            if not classifier.empty():
                return classifier
    except Exception:
        pass

    # Если не нашли - скачиваем с GitHub
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

# Инициализация YOLO
model = YOLO("yolov8l.pt")  # Модель для распознавания тренажеров
GYM_EQUIPMENT_CLASSES = ["chair", "bench"]

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
        "1. Распознавать тренажеры на фото (просто отправь фото)\n"
        "2. Запоминать и распознавать лица (используй /nameface <имя> чтобы назвать лицо)"
    )


async def list_faces(update: Update, context: ContextTypes.DEFAULT_TYPE):
    face_db = load_face_database()

    if not face_db["names"]:
        await update.message.reply_text("Нет сохраненных лиц.")
        return

    # Формируем список всех имен
    faces_list = "\n".join(
        f"{i + 1}. {name}" for i, name in enumerate(face_db["names"])
    )

    await update.message.reply_text(
        "Сохраненные лица:\n" + faces_list,
        parse_mode='Markdown'
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Скачиваем фото
        photo_file = await update.message.photo[-1].get_file()
        photo_path = f"temp_{update.message.message_id}.jpg"
        await photo_file.download_to_drive(photo_path)

        # Проверяем на тренажеры
        results = model(photo_path)
        class_name_pr = ""
        found_equipment = False

        for result in results:
            for box in result.boxes:
                class_name = result.names[int(box.cls)]
                class_name_pr += class_name + " "
                if class_name in GYM_EQUIPMENT_CLASSES:
                    found_equipment = True

        # Проверяем на лица с помощью OpenCV
        face_message = ""
        if face_cascade is not None:
            try:
                image = cv2.imread(photo_path)
                if image is None:
                    raise ValueError("Не удалось загрузить изображение")

                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))

                if len(faces) > 0:
                    face_db = load_face_database()
                    recognized_names = []

                    for (x, y, w, h) in faces:
                        features = extract_face_features(gray, (x, y, w, h))
                        name = "Unknown"

                        for i, db_features in enumerate(face_db["features"]):
                            if compare_faces(features, db_features):
                                name = face_db["names"][i]
                                break

                        if name == "Unknown":
                            name = f"User_{update.message.from_user.id}_{len(face_db['features'])}"
                            face_db["features"].append(features)
                            face_db["names"].append(name)
                            save_face_database(face_db)

                        recognized_names.append(name)

                    output_path = draw_faces(photo_path, faces, recognized_names)
                    await update.message.reply_photo(
                        photo=open(output_path, "rb"),
                        caption=f"Распознанные лица: {', '.join(recognized_names)}"
                    )
                    os.remove(output_path)
                    face_message = f"\n\nНайдены лица: {', '.join(recognized_names)}"
                else:
                    face_message = "\n\nЛица не обнаружены."
            except Exception as e:
                print(f"Ошибка при обработке лиц: {e}")
                face_message = "\n\nОшибка при распознавании лиц."
        else:
            face_message = "\n\nРаспознавание лиц недоступно (классификатор не загружен)."

        # Отправляем результат
        response = f"{'✅ Да' if found_equipment else '❌ Нет'}, на фото {'есть тренажёр' if found_equipment else 'не обнаружено тренажёров'}! {class_name_pr}{face_message}"
        await update.message.reply_text(response)

    except Exception as e:
        print(f"Ошибка при обработке фото: {e}")
        await update.message.reply_text("Произошла ошибка при обработке фото. Пожалуйста, попробуйте еще раз.")
    finally:
        if os.path.exists(photo_path):
            os.remove(photo_path)


async def rename_face(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /renameface <номер> <новое_имя>\n"
            "Пример: /renameface 2 NewName\n\n"
            "Сначала посмотрите номера лиц через /listfaces"
        )
        return

    try:
        face_num = int(context.args[0]) - 1  # Переводим в 0-based индекс
        new_name = ' '.join(context.args[1:])

        face_db = load_face_database()

        if face_num < 0 or face_num >= len(face_db["names"]):
            await update.message.reply_text("Неверный номер лица!")
            return

        old_name = face_db["names"][face_num]
        face_db["names"][face_num] = new_name
        save_face_database(face_db)

        await update.message.reply_text(
            f"Лицо успешно переименовано:\n"
            f"Было: {old_name}\n"
            f"Стало: {new_name}"
        )

    except ValueError:
        await update.message.reply_text("Номер должен быть целым числом!")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)}")


async def clear_faces(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Подтверждение через клавиатуру
    keyboard = [
        [InlineKeyboardButton("Да, очистить", callback_data="clear_confirm")],
        [InlineKeyboardButton("Отмена", callback_data="clear_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Вы уверены, что хотите удалить ВСЕ сохранённые лица?",
        reply_markup=reply_markup
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "clear_confirm":
        # Создаём новую чистую базу
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
    TOKEN = "8006388827:AAGg4xPDWHjQ8aaS30-fSy97YK7jBUUabgQ"  # Замените на ваш реальный токен
    try:
        app = Application.builder().token(TOKEN).build()

        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("nameface", name_face))
        app.add_handler(CommandHandler("listfaces", list_faces))
        app.add_handler(CommandHandler("renameface", rename_face))
        app.add_handler(CommandHandler("clearfaces", clear_faces))
        app.add_handler(CommandHandler("gym_task", gym_task))
        app.add_handler(CallbackQueryHandler(button_handler))

        print("Бот запущен...")
        app.run_polling()
    except Exception as e:
        print(f"Ошибка запуска бота: {e}")
