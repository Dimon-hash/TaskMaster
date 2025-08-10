# database.py
import logging
from typing import Optional

import asyncpg
from config import settings

logger = logging.getLogger(__name__)


class Database:
    """
    Обертка над asyncpg-пулом:
      - init() — создаёт пул и схему БД (таблицы/миграции)
      - acquire() — синхронный метод, возвращает КОНТЕКСТ-МЕНЕДЖЕР пула (!!!)
      - truncate_all() — очистка данных (TRUNCATE)
      - drop_all() — удаление таблиц (DROP)
      - close() — закрыть пул
    """
    pool: Optional[asyncpg.pool.Pool] = None

    # ---------- PUBLIC API ----------

    @classmethod
    async def init(cls) -> None:
        """Создаёт пул подключений и один раз применяет схему/миграции."""
        if cls.pool is not None:
            return

        cls.pool = await asyncpg.create_pool(dsn=str(settings.DATABASE_URL))
        async with cls.pool.acquire() as conn:
            # На всякий случай фиксируем search_path на public
            await conn.execute("SET search_path TO public")

            # Базовые таблицы
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS public.users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    face_features BYTEA,
                    face_photo BYTEA,
                    registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS public.tasks (
                    task_id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES public.users(user_id) ON DELETE CASCADE,
                    task_text TEXT,
                    status TEXT,
                    completion_date TIMESTAMP,
                    verification_photo BYTEA
                )
            """)

            # Миграции (идемпотентно)
            await cls._run_migrations(conn)

            db, usr, schema = await conn.fetchrow(
                "SELECT current_database(), current_user, current_schema()"
            )
            logger.info("DB connected: db=%s user=%s schema=%s", db, usr, schema)

        logger.info("Database initialized successfully.")

    @classmethod
    def acquire(cls):
        """
        ВАЖНО: НЕ async!
        Возвращает контекст-менеджер пула. Использование:
            async with Database.acquire() as conn:
                await conn.fetchval("SELECT 1")
        """
        if cls.pool is None:
            raise RuntimeError("Pool is not initialized. Call Database.init() first.")
        return cls.pool.acquire()

    @classmethod
    async def truncate_all(cls) -> None:
        """Очищает данные, оставляет структуру (сброс identity)."""
        if cls.pool is None:
            await cls.init()
        async with cls.pool.acquire() as conn:
            await conn.execute("SET search_path TO public")
            # порядок: сначала зависимые таблицы
            await conn.execute("TRUNCATE TABLE public.tasks, public.users RESTART IDENTITY CASCADE")
        logger.info("All tables truncated (users/tasks).")

    @classmethod
    async def drop_all(cls) -> None:
        """Удаляет таблицы. После перезапуска init() создаст их снова."""
        if cls.pool is None:
            await cls.init()
        async with cls.pool.acquire() as conn:
            await conn.execute("SET search_path TO public")
            await conn.execute("DROP TABLE IF EXISTS public.tasks CASCADE")
            await conn.execute("DROP TABLE IF EXISTS public.users CASCADE")
        logger.info("All tables dropped (users/tasks).")

    @classmethod
    async def close(cls) -> None:
        """Закрывает пул подключений."""
        if cls.pool is not None:
            await cls.pool.close()
            cls.pool = None
            logger.info("Database connection pool closed.")

    # ---------- INTERNAL ----------

    @classmethod
    async def _run_migrations(cls, conn: asyncpg.Connection) -> None:
        """
        Здесь — идемпотентные миграции. Добавляй новые ALTER’ы вниз.
        """
        # 1) Добавить колонку training_program, если её ещё нет
        await conn.execute("""
            ALTER TABLE public.users
            ADD COLUMN IF NOT EXISTS training_program TEXT
        """)

    @classmethod
    async def drop(cls):
        # alias для совместимости со старым кодом
        await cls.drop_all()

    @classmethod
    async def truncate(cls):
        # alias для совместимости со старым кодом
        await cls.truncate_all()