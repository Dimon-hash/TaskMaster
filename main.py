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

    # 2) FIX для Windows/Python 3.12: корректная политика event loop
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # 3) Инициализируем БД до старта приложения
    asyncio.run(Database.init())

    # 4) Таймзона и очередь задач (zoneinfo вместо pytz)
    TZ = ZoneInfo(getattr(settings, "TIMEZONE", "Europe/Moscow"))
    jq = JobQueue()

    # 5) Приложение
    app = (
        Application.builder()
        .token(settings.TELEGRAM_TOKEN)
        .job_queue(jq)                   # прокидываем JobQueue
        .defaults(Defaults(tzinfo=TZ))   # таймзона для job_queue/дат
        .build()
    )

    # 6) Хендлеры
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handlers.handle_webapp_data))

    app.add_handler(CommandHandler('start', handlers.start))
    app.add_handler(CommandHandler('gym_task', handlers.gym_task))
    app.add_handler(CommandHandler('profile', handlers.profile))
    app.add_handler(CommandHandler('sendphoto', handlers.send_photo))
    # app.add_handler(CommandHandler('setup_reminders', handlers.setup_reminders)) обновить время тренировок
    app.add_handler(CommandHandler('delete_db', handlers.delete_db))

    # app.add_handler(MessageHandler(filters.Regex("^🏋️ Получить задание$"), handlers.gym_task))
    app.add_handler(MessageHandler(filters.Regex("^📊 Профиль$"), handlers.profile))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

    # app.add_handler(CallbackQueryHandler(handlers.menu_callback))

    # (опционально) общий обработчик ошибок
    if hasattr(handlers, "error_handler"):
        app.add_error_handler(handlers.error_handler)

    logger.info("Бот запускается...")

    # 7) Надёжный запуск под Windows: руками запускаем цикл и polling
    async def _runner():
        await app.initialize()
        await app.start()
        await app.updater.start_polling()  # PTB v21+: это корутина
        try:
            await asyncio.Event().wait()   # держим процесс живым
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    asyncio.run(_runner())


if __name__ == '__main__':
    main()
