# main.py
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
    JobQueue,
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

    app.add_handler(CommandHandler('start', handlers.start))
    app.add_handler(CommandHandler('profile', handlers.profile))
    if hasattr(handlers, "send_photo"):
        app.add_handler(CommandHandler('sendphoto', handlers.send_photo))
    if hasattr(handlers, "clear_db"):
        app.add_handler(CommandHandler("clear_db", handlers.clear_db))
    if hasattr(handlers, "delete_db"):
        app.add_handler(CommandHandler('delete_db', handlers.delete_db))
    if hasattr(handlers, "reminders"):
        app.add_handler(CommandHandler("reminders", handlers.reminders))

    if hasattr(handlers, "handle_photo"):
        app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo))
    app.add_handler(MessageHandler(filters.Regex("^📊 Профиль$"), handlers.profile))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

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
