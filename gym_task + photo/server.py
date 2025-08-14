import os, hmac, json, urllib.parse, time
from hashlib import sha256
from uuid import uuid4

from fastapi import FastAPI, UploadFile, Form, HTTPException, Request
from fastapi.responses import Response, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import aiofiles

# --- BOT_TOKEN: берём из окружения, иначе из config.settings ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    try:
        from config import settings
        BOT_TOKEN = str(settings.TELEGRAM_TOKEN)
    except Exception:
        BOT_TOKEN = None

if not BOT_TOKEN or not BOT_TOKEN.strip():
    raise RuntimeError("BOT_TOKEN is not set. Export env var BOT_TOKEN or define config.settings.TELEGRAM_TOKEN")

# Разрешить тест в обычном браузере без Telegram при ALLOW_PLAIN_BROWSER=1
ALLOW_PLAIN_BROWSER = os.environ.get("ALLOW_PLAIN_BROWSER", "0") == "1"

TMP_DIR = os.environ.get("TMP_DIR", "./tmp")
os.makedirs(TMP_DIR, exist_ok=True)

# гарантируем, что папка static есть (чтобы не падать при монтировании)
os.makedirs("static", exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# token -> (user_id, path, exp_ts)
TOKENS: dict[str, tuple[int, str, float]] = {}
TTL = 600  # 10 минут


def verify_init_data(init_data: str) -> dict | None:
    """
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    secret_key = HMAC_SHA256("WebAppData", bot_token)
    check: hash == HMAC_SHA256(data_check_string, secret_key)
    """
    try:
        if not init_data:
            return None

        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_received = parsed.pop("hash", None)
        if not hash_received:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

        secret_key = hmac.new(
            key=b"WebAppData",
            msg=BOT_TOKEN.encode("utf-8"),
            digestmod=sha256
        ).digest()

        calc_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode("utf-8"),
            digestmod=sha256
        ).hexdigest()

        if not hmac.compare_digest(calc_hash, hash_received):
            return None

        # (опционально) свежесть
        try:
            auth_date = int(parsed.get("auth_date", "0"))
            if auth_date and (time.time() - auth_date > 24 * 3600):
                return None
        except Exception:
            pass

        return json.loads(parsed.get("user", "{}") or "{}")
    except Exception:
        return None


@app.get("/")
async def index():
    # отдаем вашу страницу
    return FileResponse("static/index.html", media_type="text/html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/favicon.ico")
async def favicon():
    # чтобы не спамило 404 в логах
    return Response(status_code=204)


@app.post("/upload")
async def upload(request: Request, photo: UploadFile, initData: str = Form(default="")):
    # dev-режим: если ?dev=1 или ALLOW_PLAIN_BROWSER=1 — позволяем пустой initData
    dev = (request.query_params.get("dev") == "1") or ALLOW_PLAIN_BROWSER

    if dev and not initData:
        user = {"id": 0}  # фейковый юзер для отладки
    else:
        user = verify_init_data(initData)

    if not user or "id" not in user:
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Bad initData. Откройте через кнопку в Telegram ИЛИ используйте ?dev=1 / ALLOW_PLAIN_BROWSER=1 для отладки."
            }
        )

    token = str(uuid4())
    path = os.path.join(TMP_DIR, f"{token}.jpg")

    async with aiofiles.open(path, "wb") as f:
        while True:
            chunk = await photo.read(1 << 20)
            if not chunk:
                break
            await f.write(chunk)

    TOKENS[token] = (int(user["id"]), path, time.time() + TTL)
    return {"token": token}


@app.get("/pull")
async def pull(token: str):
    item = TOKENS.pop(token, None)
    if not item:
        raise HTTPException(404, "token expired or not found")
    _, path, exp = item

    if time.time() > exp:
        try:
            os.remove(path)
        except Exception:
            pass
        raise HTTPException(404, "token expired")

    if not os.path.exists(path):
        raise HTTPException(410, "file gone")

    async with aiofiles.open(path, "rb") as f:
        content = await f.read()

    try:
        os.remove(path)
    except Exception:
        pass

    return Response(content, media_type="image/jpeg")
