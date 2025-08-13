from pydantic_settings import BaseSettings
from pydantic import AnyUrl
from pathlib import Path

class Settings(BaseSettings):
    TELEGRAM_TOKEN: str
    DATABASE_URL: AnyUrl
    OPENAI_API_KEY: str
    OPENAI_BASE_URL: AnyUrl
    TEMP_DIR: Path = Path("temp")
    MAX_PHOTO_SIZE: int = 5 * 1024 * 1024
    ADMIN_ID: int = 1670925755
    WEBAPP_URL: AnyUrl = "https://breath-phantom-em-mercy.trycloudflare.com"
    WEBAPP_API_PULL_URL: AnyUrl = "https://breath-phantom-em-mercy.trycloudflare.com"

    class Config:
        env_file = ".env.TaskMaster"
        env_file_encoding = "utf-8"

settings = Settings()
settings.TEMP_DIR.mkdir(parents=True, exist_ok=True)