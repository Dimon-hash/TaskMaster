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
    # your config module
from database import Database
    # your DB module
import handlers
    # your handlers module


def main():
    # 1) Логи
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    logger.info("Бот запускается...")

    # 2) Windows / Python 3.12: корректная политика + свой event loop
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 3) Инициализация БД в текущем loop’е
    loop.run_until_complete(Database.init())

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

    # Данные из WebApp (если есть)
    if hasattr(handlers, "handle_webapp_data"):
        app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handlers.handle_webapp_data))

    # Команды
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

    # Фото (и документ image/*) во время регистрации
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            handlers.register_photo
        )
    )

    # >>> Порядок callback’ов ВАЖЕН! <<<
    # 1) Депозитные кнопки (после выполнения окна / при списании)
    app.add_handler(
        CallbackQueryHandler(
            handlers.deposit_callback,
            pattern=r"^(depwin_|depforf_)",  # depwin_repeat, depwin_change_amount, depforf_restart и т.п.
            block=True,
        )
    )

    # 2) Регистрационные инлайны (тумблеры дней/времени/отдыха/длительности + dep_ok/dep_custom)
    app.add_handler(
        CallbackQueryHandler(
            handlers.register_callback,
            pattern=r"^(days_|time_|rest|dur_|dep_(ok|custom)$)",
            block=True,
        )
    )

    # 3) Прочие callback’и меню — как фолбэк
    if hasattr(handlers, "menu_callback"):
        app.add_handler(CallbackQueryHandler(handlers.menu_callback, block=False))

    # Кнопка "Профиль"
    app.add_handler(MessageHandler(filters.Regex("^📊 Профиль$"), handlers.profile))

    # Текстовый роутер (последним)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

    # 7) Запуск polling
    app.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
