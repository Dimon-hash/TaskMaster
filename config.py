# config.py
from pydantic_settings import BaseSettings
from pydantic import AnyUrl, HttpUrl, field_validator
from pathlib import Path
from urllib.parse import urlparse, quote

class Settings(BaseSettings):
    TELEGRAM_TOKEN: str
    DATABASE_URL: AnyUrl
    OPENAI_API_KEY: str
    OPENAI_BASE_URL: AnyUrl

    TEMP_DIR: Path = Path("temp")
    MAX_PHOTO_SIZE: int = 5 * 1024 * 1024
    ADMIN_ID: int = 1670925755

    WEBAPP_ORIGIN: HttpUrl | None = None

    @field_validator("WEBAPP_ORIGIN")
    @classmethod
    def _normalize_origin(cls, v):
        if v is None:
            return v
        s = str(v).rstrip("/")
        # принудительно https, если вдруг кто-то поставит http
        if s.startswith("http://"):
            s = "https://" + s[len("http://"):]
        return s

    # --------- Готовые ссылки ---------
    def _base(self) -> str:
        base = self.WEBAPP_ORIGIN or "http://127.0.0.1:8000"
        # локалка остаётся http, но в проде мы выше уже сделали https
        return base

    @property
    def WEBAPP_URL(self) -> str:
        return f"{self._base()}/"

    @property
    def WEBAPP_API_UPLOAD_URL(self) -> str:
        return f"{self._base()}/upload"

    @property
    def WEBAPP_API_PULL_URL(self) -> str:
        return f"{self._base()}/pull"

    def make_pull_url(self, token: str) -> str:
        return f"{self._base()}/pull?token={quote(token)}"

    @property
    def TELEGRAM_WEBAPP_DOMAIN(self) -> str:
        return urlparse(self._base()).netloc

    class Config:
        env_file = ".env.TaskMaster"
        env_file_encoding = "utf-8"

settings = Settings()
settings.TEMP_DIR.mkdir(parents=True, exist_ok=True)
