import asyncpg
from config import settings
import logging

logger = logging.getLogger(__name__)

class Database:
    pool: asyncpg.pool.Pool | None = None

    @classmethod
    async def init(cls):
        if cls.pool is None:
            cls.pool = await asyncpg.create_pool(dsn=str(settings.DATABASE_URL))
            async with cls.pool.acquire() as conn:
                # Создаём таблицу пользователей
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        last_name TEXT,
                        face_features BYTEA,
                        face_photo BYTEA,
                        training_program TEXT,
                        registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Создаём таблицу задач
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                        task_text TEXT,
                        status TEXT,
                        completion_date TIMESTAMP,
                        verification_photo BYTEA
                    )
                """)
            logger.info("Database initialized successfully.")

    @classmethod
    async def close(cls):
        if cls.pool is not None:
            await cls.pool.close()
            cls.pool = None
            logger.info("Database connection pool closed.")

    @classmethod
    async def acquire(cls):
        if cls.pool is None:
            await cls.init()
        return cls.pool.acquire()  # Это корректно, async with сам откроет соединение
