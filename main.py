import logging
import sys
import asyncio
from zoneinfo import ZoneInfo

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    Defaults,
)

from config import settings
from database import Database
import handlers


def main():
    # 1) Логи
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    # 2) Windows / Python 3.12 — корректная политика цикла
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # 3) Инициализация БД
    asyncio.run(Database.init())

    # 4) Таймзона
    TZ = ZoneInfo(getattr(settings, "TIMEZONE", "Europe/Moscow"))

    # 5) Приложение
    app = (
        Application.builder()
        .token(settings.TELEGRAM_TOKEN)
        .defaults(Defaults(tzinfo=TZ))
        .build()
    )

    # 6) Хендлеры
    if hasattr(handlers, "handle_webapp_data"):
        app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handlers.handle_webapp_data))

    # /start запускает онбординг
    app.add_handler(CommandHandler('start', handlers.start))
    app.add_handler(CommandHandler('profile', handlers.profile))

    if hasattr(handlers, "clear_db"):
        app.add_handler(CommandHandler("clear_db", handlers.clear_db))
    if hasattr(handlers, "delete_db"):
        app.add_handler(CommandHandler('delete_db', handlers.delete_db))
    if hasattr(handlers, "reminders"):
        app.add_handler(CommandHandler("reminders", handlers.reminders))

    # 📸 Приём фото в онбординге: как photo и как document(image/*)
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
                                   handlers.register_photo))

    # Инлайн-кнопки онбординга: выбор дней, режима длительности и депозита
    # ВАЖНО: добавили dur_ чтобы кнопки «Да/Нет (одинаковая/разная)» работали
    app.add_handler(CallbackQueryHandler(handlers.register_callback, pattern=r"^(day_|dep_|dur_)"))

    # Кнопка "Профиль"
    app.add_handler(MessageHandler(filters.Regex("^📊 Профиль$"), handlers.profile))

    # Текст: сначала онбординг, остальное — в общий роутер
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

    # Совместимость: если есть своё меню
    if hasattr(handlers, "menu_callback"):
        app.add_handler(CallbackQueryHandler(handlers.menu_callback))

    logger.info("Бот запускается...")

    # 7) Создаём и назначаем текущий event loop (фикс для Py3.12)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 8) Запуск polling
    app.run_polling()  # без allowed_updates — примет все по умолчанию


if __name__ == '__main__':
    main()
