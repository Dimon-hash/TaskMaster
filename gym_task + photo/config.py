# config.py
from pydantic_settings import BaseSettings
from pydantic import AnyUrl, HttpUrl, field_validator
from pathlib import Path
from urllib.parse import urlparse, quote  # <-- добавил quote

class Settings(BaseSettings):
    TELEGRAM_TOKEN: str
    DATABASE_URL: AnyUrl
    OPENAI_API_KEY: str
    OPENAI_BASE_URL: AnyUrl

    TEMP_DIR: Path = Path("temp")
    MAX_PHOTO_SIZE: int = 5 * 1024 * 1024
    ADMIN_ID: int = 1670925755

    # Базовый origin без завершающего / (например, https://bot.example.com)
    # Если пусто — локально http://127.0.0.1:8000
    WEBAPP_ORIGIN: HttpUrl | None = None

    @field_validator("WEBAPP_ORIGIN")
    @classmethod
    def _strip_trailing_slash(cls, v):
        if v is None:
            return v
        s = str(v)
        return s[:-1] if s.endswith("/") else s

    # --------- Готовые ссылки для фронта/бота ---------
    @property
    def WEBAPP_URL(self) -> str:
        base = self.WEBAPP_ORIGIN or "http://127.0.0.1:8000"
        return f"{base}/"

    @property
    def WEBAPP_API_UPLOAD_URL(self) -> str:
        """Абсолютный URL для POST /upload (если понадобится на бэке/клиенте)."""
        base = self.WEBAPP_ORIGIN or "http://127.0.0.1:8000"
        return f"{base}/upload"

    @property
    def WEBAPP_API_PULL_URL(self) -> str:
        """Абсолютный URL для GET /pull (без query)."""
        base = self.WEBAPP_ORIGIN or "http://127.0.0.1:8000"
        return f"{base}/pull"

    def make_pull_url(self, token: str) -> str:
        """URL для скачивания фото: /pull?token=..."""
        base = self.WEBAPP_ORIGIN or "http://127.0.0.1:8000"
        return f"{base}/pull?token={quote(token)}"

    # Удобно для BotFather (/setdomain)
    @property
    def TELEGRAM_WEBAPP_DOMAIN(self) -> str:
        base = self.WEBAPP_ORIGIN or "http://127.0.0.1:8000"
        return urlparse(base).netloc

    class Config:
        env_file = ".env.TaskMaster"
        env_file_encoding = "utf-8"

settings = Settings()
settings.TEMP_DIR.mkdir(parents=True, exist_ok=True)
