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

from config import settings        # ваш модуль конфигурации
from database import Database      # ваш модуль БД
import handlers                    # ваш модуль с хендлерами


def main():
    # 1) Логи
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    logger = logging.getLogger(__name__)
    logger.info("Бот запускается...")

    # 2) Windows / Python 3.12: корректная политика цикла событий
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    # 3) Создаём и устанавливаем event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 4) Инициализация БД в текущем loop’е
    loop.run_until_complete(Database.init())

    # 5) Таймзона по умолчанию
    tz_name = getattr(settings, "TIMEZONE", "Europe/Moscow")
    TZ = ZoneInfo(tz_name)

    # 6) Приложение PTB
    app = (
        Application.builder()
        .token(settings.TELEGRAM_TOKEN)
        .defaults(Defaults(tzinfo=TZ))
        .build()
    )

    # 7) Хендлеры

    # Данные из WebApp (если используются)
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

    # Фото (и документ image/*)
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            handlers.register_photo
        )
    )

    # >>> Порядок callback’ов ВАЖЕН! <<<
    # 1) Депозитные кнопки (после выполнения окна / при списании)
    #    depwin_repeat, depwin_change_amount, depwin_change_sched, depwin_later, depforf_restart
    app.add_handler(
        CallbackQueryHandler(
            handlers.deposit_callback,
            pattern=r"^(depwin_|depforf_)",
            block=True,
        )
    )

    # 2) Регистрационные инлайны:
    #    - экран 1 → экран 2: ob_next            ← ДОБАВЛЕНО
    #    - старт 3 вопросов: qa_begin
    #    - тумблеры дней/времени/отдыха/длительности: days_*, time_*, rest*, dur_*
    #    - выбор депозита: dep_ok / dep_custom
    app.add_handler(
        CallbackQueryHandler(
            handlers.register_callback,
            pattern=r"^(ob_next$|qa_begin$|days_|time_|rest|dur_|dep_(ok|custom)$)",
            block=True,
        )
    )

    # 3) Прочие callback’и меню — как фолбэк (если есть)
    if hasattr(handlers, "menu_callback"):
        app.add_handler(CallbackQueryHandler(handlers.menu_callback, block=False))

    # Кнопка "Профиль" из ReplyKeyboard
    app.add_handler(MessageHandler(filters.Regex(r"^📊 Профиль$"), handlers.profile))

    # Текстовый роутер (идёт последним)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

    # 8) Запуск polling
    logger.info("Стартуем polling...")
    app.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
