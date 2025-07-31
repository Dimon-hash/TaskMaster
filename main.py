import os
import pickle
import cv2
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from ultralytics import YOLO
import face_recognition

# Инициализация моделей
model = YOLO("yolov8l.pt")  # Модель для распознавания тренажеров

GYM_EQUIPMENT_CLASSES = [
    "chair",
    "bench"
]

# Настройки для распознавания лиц
FACE_DATA_DIR = "face_data"
os.makedirs(FACE_DATA_DIR, exist_ok=True)


# Функции для работы с лицами
def load_face_database():
    try:
        with open(os.path.join(FACE_DATA_DIR, "faces.pkl"), "rb") as f:
            return pickle.load(f)
    except (FileNotFoundError, EOFError):
        return {"encodings": [], "names": []}


def save_face_database(database):
    with open(os.path.join(FACE_DATA_DIR, "faces.pkl"), "wb") as f:
        pickle.dump(database, f)


def draw_faces(image_path, face_locations, names):
    image = cv2.imread(image_path)
    for (top, right, bottom, left), name in zip(face_locations, names):
        cv2.rectangle(image, (left, top), (right, bottom), (0, 255, 0), 2)
        cv2.putText(image, name, (left, top - 10),
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


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Скачиваем фото
    photo_file = await update.message.photo[-1].get_file()
    photo_path = f"temp_{update.message.message_id}.jpg"
    await photo_file.download_to_drive(photo_path)

    # Сначала проверяем на тренажеры
    results = model(photo_path)

    class_name_pr = ""
    found_equipment = False
    for result in results:
        for box in result.boxes:
            class_name = result.names[int(box.cls)]
            class_name_pr += class_name + " "
            if class_name in GYM_EQUIPMENT_CLASSES:
                found_equipment = True

    # Затем проверяем на лица
    image = face_recognition.load_image_file(photo_path)
    face_locations = face_recognition.face_locations(image)
    face_message = ""

    if face_locations:
        face_encodings = face_recognition.face_encodings(image, face_locations)
        face_db = load_face_database()
        recognized_names = []

        for face_encoding in face_encodings:
            matches = face_recognition.compare_faces(face_db["encodings"], face_encoding)
            name = "Unknown"

            if True in matches:
                first_match_index = matches.index(True)
                name = face_db["names"][first_match_index]
            else:
                name = f"User_{update.message.from_user.id}_{len(face_db['encodings'])}"
                face_db["encodings"].append(face_encoding)
                face_db["names"].append(name)
                save_face_database(face_db)

            recognized_names.append(name)

        output_path = draw_faces(photo_path, face_locations, recognized_names)
        await update.message.reply_photo(
            photo=open(output_path, "rb"),
            caption=f"Распознанные лица: {', '.join(recognized_names)}"
        )
        os.remove(output_path)
        face_message = f"\n\nНайдены лица: {', '.join(recognized_names)}"
    else:
        face_message = "\n\nЛица не обнаружены."

    # Отправляем результат
    if found_equipment:
        await update.message.reply_text(f"✅ Да, на фото есть тренажёр! {class_name_pr}{face_message}")
    else:
        await update.message.reply_text(f"❌ Нет, на фото не обнаружено тренажёров. {class_name_pr}{face_message}")

    os.remove(photo_path)


async def name_face(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /nameface <имя>")
        return

    face_db = load_face_database()
    if not face_db["encodings"]:
        await update.message.reply_text("Нет сохраненных лиц.")
        return

    new_name = ' '.join(context.args)
    face_db["names"][-1] = new_name
    save_face_database(face_db)
    await update.message.reply_text(f"Последнее лицо сохранено как: {new_name}")


if __name__ == "__main__":
    TOKEN = "8006388827:AAGg4xPDWHjQ8aaS30-fSy97YK7jBUUabgQ"
    app = Application.builder().token(TOKEN).build()

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nameface", name_face))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("Бот запущен...")
    app.run_polling()
