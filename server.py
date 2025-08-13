# server.py
import os, hmac, json, urllib.parse, time
from hashlib import sha256
from uuid import uuid4

from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
import aiofiles

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN env var")

TMP_DIR = os.environ.get("TMP_DIR", "./tmp")
os.makedirs(TMP_DIR, exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

TOKENS: dict[str, tuple[int, str, float]] = {}
TTL = 600  # 10 минут

def verify_init_data(init_data: str) -> dict | None:
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_received = parsed.pop("hash", None)
        if not hash_received:
            return None

        # 1) data_check_string: сортируем и склеиваем k=v по \n
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

        # 2) secret_key = HMAC_SHA256("WebAppData", BOT_TOKEN)
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=BOT_TOKEN.encode("utf-8"),
            digestmod=sha256
        ).digest()

        # 3) check hash = HMAC_SHA256(secret_key, data_check_string)
        calc_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode("utf-8"),
            digestmod=sha256
        ).hexdigest()

        if not hmac.compare_digest(calc_hash, hash_received):
            return None

        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

@app.get("/")
async def index():
    return FileResponse("static/index.html", media_type="text/html")

@app.post("/upload")
async def upload(photo: UploadFile, initData: str = Form(...)):
    user = verify_init_data(initData)
    if not user or "id" not in user:
        raise HTTPException(403, "Bad initData")
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
        try: os.remove(path)
        except Exception: pass
        raise HTTPException(404, "token expired")
    if not os.path.exists(path):
        raise HTTPException(410, "file gone")
    async with aiofiles.open(path, "rb") as f:
        content = await f.read()
    try: os.remove(path)
    except Exception: pass
    return Response(content, media_type="image/jpeg")
