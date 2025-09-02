import os
import sys
import asyncio
import logging
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

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

load_dotenv()


async def _post_init(app: Application):
    """Инициализация перед стартом polling."""
    log = logging.getLogger(__name__)
    log.info("Инициализация приложения...")

    # 1) БД
    await Database.init()

    # 2) На всякий случай убираем webhook перед polling
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared before polling.")
    except Exception:
        log.exception("delete_webhook failed (not fatal)")

    # 3) Гарантируем JobQueue (если забыли в билдере)
    if app.job_queue is None:
        jq = JobQueue()
        jq.set_application(app)
        app.job_queue = jq

    # 4) Пересоздать напоминания из БД (если у тебя есть такая функция)
    try:
        if hasattr(handlers, "reschedule_all_users"):
            await handlers.reschedule_all_users(app)
            log.info("reschedule_all_users completed.")
    except Exception:
        log.exception("reschedule_all_users failed (not fatal)")


async def _post_shutdown(app: Application):
    """Аккуратно закрываем ресурсы."""
    log = logging.getLogger(__name__)
    try:
        await Database.close()
        log.info("Database connection closed.")
    except Exception:
        log.exception("Database.close() failed")


def main():
    # Логи
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    log = logging.getLogger(__name__)
    log.info("Бот запускается...")

    # Windows / Python 3.12: корректная политика цикла событий
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    # Проверка токена — частая причина падений
    token = getattr(settings, "TELEGRAM_TOKEN", None)
    if not token or not str(token).strip():
        log.error("TELEGRAM_TOKEN пуст. Проверь .env / config.settings.")
        sys.exit(1)

    # Таймзона
    tz_name = getattr(settings, "TIMEZONE", "Europe/Moscow")
    TZ = ZoneInfo(tz_name)
    log.info("TZ=%s", tz_name)

    # Application
    app = (
        Application.builder()
        .token(settings.TELEGRAM_TOKEN)
        .defaults(Defaults(tzinfo=TZ))
        .job_queue(JobQueue())         # сразу добавим JobQueue
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # --- Хендлеры ---

    # WebApp data (tz/фото-токены и т.д.)
    if hasattr(handlers, "handle_webapp_data"):
        app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handlers.handle_webapp_data))

    # Базовые команды
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

    # Приём фото (если используешь отдельный регистратор фото)
    if hasattr(handlers, "register_photo"):
        app.add_handler(
            MessageHandler(
                (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
                handlers.register_photo
            )
        )

    # Кнопки по депозиту (окно выполнено/списание)
    app.add_handler(
        CallbackQueryHandler(
            handlers.deposit_callback,
            pattern=r"^(depwin_|depforf_)",
            block=True,
        )
    )

    # РЕГИСТРАЦИЯ / ОНБОРДИНГ — полный pattern, чтобы ловить ВСЕ callback_data
    app.add_handler(
        CallbackQueryHandler(
            handlers.register_callback,
            pattern=(
                r"^(ob_next$|qa_begin$|"
                r"days_start$|"
                r"days_(toggle|clear|done)(?::.*)?|"   # days_toggle:mon, days_done
                r"time_pick:.*|"                       # time_pick:mon:07:00
                r"rest(?::|_).*|"                      # rest:60, rest_custom
                r"dur_(?:same|diff|common_.*|pd_set:.*|pd_custom:.*)|"
                r"plan_(?:add|done):.*|"               # plan_add:mon / plan_done:mon
                r"day_(?:edit|ok):.*|"                 # day_edit:mon / day_ok:mon
                r"dep_(?:ok|custom)$)"
            ),
            block=True,
        )
    )

    # Прочее меню (если есть)
    if hasattr(handlers, "menu_callback"):
        app.add_handler(CallbackQueryHandler(handlers.menu_callback, block=False))

    # Кнопка «Профиль» в ReplyKeyboard
    app.add_handler(MessageHandler(filters.Regex(r"Профиль$"), handlers.profile))

    # Текстовые ответы (регистрация/мастера/общение)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

    # --- Старт Polling (PTB сам ловит SIGINT/SIGTERM и корректно завершает) ---
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Fatal error in main()")
        raise
