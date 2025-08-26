# main.py
import os
import sys
import asyncio
import logging
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse, JSONResponse

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, Defaults,
)

# ---- твои модули
from database import Database
import handlers


# ===================== ENV =====================
TELEGRAM_TOKEN: Optional[str] = os.getenv("TELEGRAM_TOKEN")
PUBLIC_BASE_URL: str = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET_TOKEN", "supersecret")
TIMEZONE_NAME: str = os.getenv("TIMEZONE", "Europe/Moscow")

# Конечный путь вебхука. Пример: /tg/webhook/supersecret
WEBHOOK_PATH = f"/tg/webhook/{WEBHOOK_SECRET}"


# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("main")


# ===================== FASTAPI =====================
app = FastAPI(title="TaskMaster Bot API")

# (опционально) — если ты открываешь WebApp со своего фронта/домена
# можно добавить CORS на нужные источники
allow_origins = []
if PUBLIC_BASE_URL.startswith("https://"):
    # разрешим самому себе (можно расширить списком доменов)
    allow_origins = [PUBLIC_BASE_URL]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# PTB Application держим глобально (живёт пока живёт процесс)
tg_app: Optional[Application] = None
TZ = ZoneInfo(TIMEZONE_NAME)


# ===================== ROUTES =====================
@app.get("/health")
async def health():
    return PlainTextResponse("ok")


@app.get("/")
async def root():
    # Небольшой пинг-эндпоинт, чтобы видеть, что сервис жив
    return JSONResponse({"ok": True, "service": "taskmaster-bot", "webhook_path": WEBHOOK_PATH})


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """
    Точка входа вебхука Telegram. Render будет слать сюда POST от Telegram.
    """
    # Безопасность: сверяем секретный заголовок Telegram
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return PlainTextResponse("forbidden", status_code=403)

    if tg_app is None:
        return PlainTextResponse("not ready", status_code=503)

    try:
        data = await request.json()
    except Exception:
        return PlainTextResponse("bad request", status_code=400)

    try:
        update = Update.de_json(data, tg_app.bot)
    except Exception as e:
        log.exception("Update.de_json failed: %s", e)
        return PlainTextResponse("bad update", status_code=400)

    await tg_app.process_update(update)
    return PlainTextResponse("ok")


# ===================== STARTUP / SHUTDOWN =====================
@app.on_event("startup")
async def on_startup():
    """
    Инициализирует БД и Telegram Application, регистрирует хендлеры
    и ставит вебхук на PUBLIC_BASE_URL + WEBHOOK_PATH.
    """
    global tg_app

    # Windows-политика (не мешает на Linux/Render)
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
        log.error("TELEGRAM_TOKEN не задан — бот не будет запущен")
        return

    # Собираем PTB App
    tg_app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .defaults(Defaults(tzinfo=TZ))
        .build()
    )

    # ---------- Регистрируем твои хендлеры из handlers ----------
    # WebApp data
    if hasattr(handlers, "handle_webapp_data"):
        tg_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handlers.handle_webapp_data))

    # Команды
    if hasattr(handlers, "start"):
        tg_app.add_handler(CommandHandler("start", handlers.start))
    if hasattr(handlers, "profile"):
        tg_app.add_handler(CommandHandler("profile", handlers.profile))

    # Админ/сервисные команды (если определены)
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

    # Медиа: фото/картинки как документ
    if hasattr(handlers, "register_photo"):
        tg_app.add_handler(
            MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, handlers.register_photo)
        )

    # Callback-кнопки депозита
    if hasattr(handlers, "deposit_callback"):
        tg_app.add_handler(
            CallbackQueryHandler(
                handlers.deposit_callback,
                pattern=r"^(depwin_|depforf_)", block=True
            )
        )

    # Инлайн регистрации и пр.
    if hasattr(handlers, "register_callback"):
        tg_app.add_handler(
            CallbackQueryHandler(
                handlers.register_callback,
                pattern=r"^(ob_next$|qa_begin$|days_|time_|rest|dur_|dep_(ok|custom)$)",
                block=True
            )
        )

    # Доп. меню-коллбэки, если есть
    if hasattr(handlers, "menu_callback"):
        tg_app.add_handler(CallbackQueryHandler(handlers.menu_callback, block=False))

    # Кнопка «Профиль» (текст из клавиатуры)
    if hasattr(handlers, "profile"):
        tg_app.add_handler(MessageHandler(filters.Regex(r"^📊 Профиль$"), handlers.profile))

    # Текстовый роутер — последним
    if hasattr(handlers, "handle_text"):
        tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

    # Инициализируем PTB (без polling)
    await tg_app.initialize()

    # Ставим вебхук, если указан публичный базовый URL
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
        log.warning("PUBLIC_BASE_URL пуст — вебхук НЕ включён (на Render вебхук обязателен).")


@app.on_event("shutdown")
async def on_shutdown():
    """
    Корректно завершаем PTB и (опционально) снимаем вебхук.
    """
    global tg_app
    try:
        if tg_app is not None:
            try:
                # не обязательно снимать вебхук на Render, но на всякий случай:
                if PUBLIC_BASE_URL:
                    await tg_app.bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                pass
            try:
                await tg_app.shutdown()
            finally:
                tg_app = None
    except Exception as e:
        log.exception("shutdown error: %s", e)
