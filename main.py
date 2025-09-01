# -*- coding: utf-8 -*-
import logging
import sys
import os
import fcntl
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    Defaults,
    JobQueue,
    filters,
)

from config import settings
from database import Database
import handlers


LOCK_PATH = "/run/taskmaster/bot.lock"  # см. RuntimeDirectory=taskmaster в systemd


def acquire_single_instance_lock():
    """Простой file-lock, чтобы второй экземпляр не стартовал."""
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    except Exception:
        # не фейлим старт, просто попробуем залочиться ниже
        pass

    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another bot instance is already running. Exiting.")
        sys.exit(1)
    return lock_file  # держим ссылку до конца процесса


async def _post_init(app: Application):
    log = logging.getLogger(__name__)
    log.info("Инициализация приложения...")

    # 1) БД
    await Database.init()

    # 2) На всякий случай: убрать webhook перед polling (если был включён)
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        log.exception("delete_webhook failed (not fatal)")

    # 3) Гарантируем JobQueue (если забыли в билдере)
    if app.job_queue is None:
        jq = JobQueue()
        jq.set_application(app)
        app.job_queue = jq

    # 4) Твои отложенные задачи
    try:
        if hasattr(handlers, "reschedule_all_users"):
            await handlers.reschedule_all_users(app)
    except Exception:
        log.exception("reschedule_all_users failed (not fatal)")


async def _post_shutdown(app: Application):
    log = logging.getLogger(__name__)
    try:
        await Database.close()
    except Exception:
        log.exception("Database.close() failed")


def build_app() -> Application:
    tz_name = getattr(settings, "TIMEZONE", "Europe/Moscow")
    TZ = ZoneInfo(tz_name)

    app = (
        Application.builder()
        .token(settings.TELEGRAM_TOKEN)
        .defaults(Defaults(tzinfo=TZ))
        .job_queue(JobQueue())          # <<< важная строка, чтобы не было варнингов
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # --- Хендлеры ---
    if hasattr(handlers, "handle_webapp_data"):
        app.add_handler(
            MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handlers.handle_webapp_data)
        )

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("profile", handlers.profile))

    if hasattr(handlers, "clear_db"):
        app.add_handler(CommandHandler("clear_db", handlers.clear_db))
    if hasattr(handlers, "delete_db"):
        app.add_handler(CommandHandler("delete_db", handlers.delete_db))
    if hasattr(handlers, "reminders"):
        app.add_handler(CommandHandler("reminders", handlers.reminders))
    if hasattr(handlers, "start_workout"):
        app.add_handler(CommandHandler("start_workout", handlers.start_workout))
    if hasattr(handlers, "end_workout"):
        app.add_handler(CommandHandler("end_workout", handlers.end_workout))

    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            handlers.register_photo,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            handlers.deposit_callback,
            pattern=r"^(depwin_|depforf_)",
            block=True,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            handlers.register_callback,
            pattern=r"^(ob_next$|qa_begin$|days_|time_|rest|dur_|dep_(ok|custom)$)",
            block=True,
        )
    )

    if hasattr(handlers, "menu_callback"):
        app.add_handler(CallbackQueryHandler(handlers.menu_callback, block=False))

    app.add_handler(MessageHandler(filters.Regex(r"Профиль$"), handlers.profile))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

    return app


def main():
    # Логи
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    log = logging.getLogger(__name__)
    log.info("Бот запускается...")

    # Проверка токена (частая причина падений)
    token = getattr(settings, "TELEGRAM_TOKEN", None)
    if not token or not str(token).strip():
        log.error("TELEGRAM_TOKEN пуст. Проверь .env / config.settings.")
        sys.exit(1)

    # Один экземпляр процесса
    lock_file = acquire_single_instance_lock()

    # (Опционально) если когда-то будешь запускать на Windows:
    # if sys.platform.startswith("win"):
    #     import asyncio
    #     try:
    #         asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    #     except Exception:
    #         pass

    app = build_app()

    # Блокирующий вызов; PTB сам обработает SIGINT/SIGTERM и корректно завершит работу
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        # stop_signals по умолчанию ловит SIGINT/SIGTERM — указывать не нужно
    )

    # Держим ссылку на лок до конца процесса
    _ = lock_file


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Fatal error in main()")
        raise
