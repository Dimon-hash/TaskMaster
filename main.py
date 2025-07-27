import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from ultralytics import YOLO


from ultralytics import RTDETR
model = RTDETR("rtdetr-l.pt")


GYM_EQUIPMENT_CLASSES = [
    # 1. Силовые тренажёры и оборудование
    "dumbbell",          # гантель
    "barbell",           # штанга
    "kettlebell",        # гиря
    "weight plate",      # блин для штанги
    "medicine ball",     # медбол
    "resistance band",   # эспандер
    "pull-up bar",       # турник
    "weight bench",      # скамья для жима
    "power rack",        # силовая рама
    "smith machine",     # тренажёр Смита
    "leg press machine", # тренажёр для жима ногами
    "cable machine",     # кроссовер (блочный тренажёр)
    "ab roller",         # ролик для пресса

    # 2. Кардиотренажёры
    "treadmill",         # беговая дорожка
    "exercise bike",     # велотренажёр
    "elliptical machine",# эллипсоид
    "rowing machine",    # гребной тренажёр
    "stair climber",     # степпер
    "spin bike",         # спинбайк

    # 3. Гимнастика и функциональный тренинг
    "yoga mat",          # коврик для йоги
    "foam roller",       # массажный ролик
    "jump rope",         # скакалка
    "gymnastic rings",   # гимнастические кольца
    "parallette bars",   # параллельные брусья
    "balance board",     # балансборд

    # 4. Игровые виды спорта
    "sports ball",       # мяч (общий)
    "basketball",        # баскетбольный мяч
    "soccer ball",       # футбольный мяч
    "volleyball",        # волейбольный мяч
    "tennis racket",     # теннисная ракетка
    "tennis ball",       # теннисный мяч
    "golf club",         # клюшка для гольфа
    "golf ball",         # мяч для гольфа
    "ping pong paddle",  # ракетка для пинг-понга
    "badminton racket",  # ракетка для бадминтона
    "hockey stick",      # клюшка для хоккея

    # 5. Зимние виды спорта
    "skis",              # лыжи
    "ski poles",         # лыжные палки
    "snowboard",         # сноуборд
    "ice skates",        # коньки

    # 6. Водные виды спорта
    "surfboard",         # серфборд
    "paddle board",      # сапборд
    "kayak",             # байдарка
    "swimming goggles",  # плавательные очки

    # 7. Экстремальные и уличные
    "skateboard",        # скейтборд
    "bmx bike",          # BMX-велосипед
    "climbing harness",  # альпинистская обвязка
    "carabiner",         # карабин


    "chair",
    "bench"
]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь мне фото, и я скажу, есть ли на нём тренажёр."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Скачиваем фото
    photo_file = await update.message.photo[-1].get_file()
    photo_path = f"temp_{update.message.message_id}.jpg"
    await photo_file.download_to_drive(photo_path)

    # Анализ фото через YOLO
    results = model(photo_path)

    # Проверяем, есть ли тренажёры
    found_equipment = False
    for result in results:
        for box in result.boxes:
            class_name = result.names[int(box.cls)]
            print(class_name)
            if class_name in GYM_EQUIPMENT_CLASSES:
                found_equipment = True
                break

    # Удаляем временный файл
    os.remove(photo_path)

    # Отправляем ответ
    if found_equipment:
        await update.message.reply_text("✅ Да, на фото есть тренажёр!")
    else:
        await update.message.reply_text("❌ Нет, на фото не обнаружено тренажёров.")


if __name__ == "__main__":
    # 3. Настройка бота
    TOKEN = "8006388827:AAGg4xPDWHjQ8aaS30-fSy97YK7jBUUabgQ"
    app = Application.builder().token(TOKEN).build()

    # 4. Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # 5. Запуск бота
    print("Бот запущен...")
    app.run_polling()