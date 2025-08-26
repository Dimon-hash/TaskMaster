# main.py (корень репо)
import os
import sys
import asyncio
import logging
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from starlette.responses import PlainTextResponse

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, Defaults,
)

# --- твои модули
from database import Database
import handlers

# ---------- Конфиг через ENV (без жёстких импортов config.py)
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
PUBLIC_BASE_URL  = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET_TOKEN", "supersecret")
TIMEZONE_NAME    = os.getenv("TIMEZONE", "Europe/Moscow")
WEBHOOK_PATH     = f"/tg/webhook/{WEBHOOK_SECRET}"

# ---------- Логи
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("main")

# ---------- FastAPI
app = FastAPI()

# PTB application держим глобально, чтобы обрабатывать апдейты
tg_app: Application | None = None
TZ = ZoneInfo(TIMEZONE_NAME)


# ---------- ROUTES
@app.get("/health")
async def health():
    return PlainTextResponse("ok")


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # Проверяем секрет Telegram
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return PlainTextResponse("forbidden", status_code=403)

    if tg_app is None:
        return PlainTextResponse("not ready", status_code=503)

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return PlainTextResponse("ok")


# ---------- STARTUP
@app.on_event("startup")
async def on_startup():
    global tg_app

    # Windows policy (не мешает на Linux/Render)
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    # Инициализация БД
    try:
        await Database.init()
        log.info("Database initialized")
    except Exception as e:
        log.exception("Database.init() failed: %s", e)

    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN пуст — бот не будет запущен")
        return

    # Собираем PTB app
    tg_app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .defaults(Defaults(tzinfo=TZ))
        .build()
    )

    # ----- Регистрируем твои хендлеры
    # WebApp data (если используешь)
    if hasattr(handlers, "handle_webapp_data"):
        tg_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handlers.handle_webapp_data))

    # Команды
    tg_app.add_handler(CommandHandler("start",   handlers.start))
    tg_app.add_handler(CommandHandler("profile", handlers.profile))

    if hasattr(handlers, "clear_db"):
        tg_app.add_handler(CommandHandler("clear_db", handlers.clear_db))
    if hasattr(handlers, "delete_db"):
        tg_app.add_handler(CommandHandler("delete_db", handlers.delete_db))
    if hasattr(handlers, "reminders"):
        tg_app.add_handler(CommandHandler("reminders", handlers.reminders))
    if hasattr(handlers, "start_workout"):
        tg_app.add_handler(CommandHandler("start_workout", handlers.start_workout))
    if hasattr(handlers, "end_workout"):
        tg_app.add_handler(CommandHandler("end_workout", handlers.end_workout))

    # Фото/документы-изображения
    tg_app.add_handler(
        MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, handlers.register_photo)
    )

    # Депозитные кнопки
    tg_app.add_handler(
        CallbackQueryHandler(
            handlers.deposit_callback,
            pattern=r"^(depwin_|depforf_)", block=True
        )
    )

    # Регистрация / инлайны
    tg_app.add_handler(
        CallbackQueryHandler(
            handlers.register_callback,
            pattern=r"^(ob_next$|qa_begin$|days_|time_|rest|dur_|dep_(ok|custom)$)",
            block=True
        )
    )

    # Прочие callback’и меню (если есть)
    if hasattr(handlers, "menu_callback"):
        tg_app.add_handler(CallbackQueryHandler(handlers.menu_callback, block=False))

    # Кнопка "Профиль"
    tg_app.add_handler(MessageHandler(filters.Regex(r"^📊 Профиль$"), handlers.profile))

    # Текстовый роутер (последним)
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

    # Инициализация PTB и установка вебхука
    await tg_app.initialize()

    if PUBLIC_BASE_URL:
        url = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"
        try:
            await tg_app.bot.set_webhook(
                url=url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query"],
            )
            log.info("Webhook set to %s", url)
        except Exception as e:
            log.exception("set_webhook failed: %s", e)
    else:
        log.warning("PUBLIC_BASE_URL пуст — вебхук НЕ включён (на Render нужен вебхук).")


# ---------- (не нужен if __name__ == '__main__' — запуском рулит Uvicorn)
