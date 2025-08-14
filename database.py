# database.py
import logging
from typing import Optional

import asyncpg
from config import settings

logger = logging.getLogger(__name__)

# В рантайме работаем без пула (одноразовые соединения) — стабильно на Windows/Py3.12.
USE_POOL_FOR_RUNTIME = False


class _DirectConn:
    """Контекст-менеджер одноразового соединения (без пула)."""
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: Optional[asyncpg.Connection] = None

    async def __aenter__(self) -> asyncpg.Connection:
        self._conn = await asyncpg.connect(dsn=self._dsn)
        await self._conn.execute("SET search_path TO public")
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        if self._conn:
            await self._conn.close()
            self._conn = None


class Database:
    """
    Обёртка над БД:
      - init() — создаёт пул и применяет схему/миграции
      - acquire() — в safe-режиме возвращает одноразовое соединение (без пула)
      - truncate_all()/drop_all() — разрушающие операции на отдельном соединении
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
                    registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    training_program TEXT,
                    training_form JSONB,
                    reminder_enabled BOOLEAN DEFAULT FALSE,
                    reminder_time TIME,
                    reminder_days TEXT[],
                    -- legacy: раньше так хранили длительность
                    reminder_duration TEXT,
                    -- актуальная длительность в минутах
                    workout_duration INT
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS public.sets (
                    set_id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES public.users(user_id) ON DELETE CASCADE,
                    photo BYTEA,
                    verified BOOLEAN DEFAULT FALSE,
                    gpt_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Миграции (идемпотентно)
            await cls._run_migrations(conn)

            row = await conn.fetchrow("SELECT current_database() AS db, current_user AS usr, current_schema() AS sch")
            logger.info("DB connected: db=%s user=%s schema=%s", row["db"], row["usr"], row["sch"])

        logger.info("Database initialized successfully.")

    @classmethod
    def acquire(cls):
        """
        Возвращает контекст-менеджер соединения.
        В safe-режиме — одноразовое подключение (без пула).
        Если нужен пул — выстави USE_POOL_FOR_RUNTIME = True.
        """
        dsn = str(settings.DATABASE_URL)
        if not USE_POOL_FOR_RUNTIME:
            return _DirectConn(dsn)

        if cls.pool is None:
            raise RuntimeError("Pool is not initialized. Call Database.init() first.")
        return cls.pool.acquire()

    @classmethod
    async def truncate_all(cls) -> None:
        """Очищает данные, оставляет структуру (сброс identity)."""
        conn = await asyncpg.connect(dsn=str(settings.DATABASE_URL))
        try:
            await conn.execute("SET search_path TO public")
            await conn.execute("TRUNCATE TABLE public.sets, public.users RESTART IDENTITY CASCADE")
            logger.info("All tables truncated (users/sets).")
        finally:
            await conn.close()

    @classmethod
    async def drop_all(cls) -> None:
        """Удаляет таблицы."""
        conn = await asyncpg.connect(dsn=str(settings.DATABASE_URL))
        try:
            await conn.execute("SET search_path TO public")
            await conn.execute("DROP TABLE IF EXISTS public.sets CASCADE")
            await conn.execute("DROP TABLE IF EXISTS public.users CASCADE")
            logger.info("All tables dropped (users/sets).")
        finally:
            await conn.close()

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
        Идемпотентные миграции для совместимости со старыми версиями.
        Добавляй новые ALTER’ы вниз.
        """
        # users — добиваем недостающие поля
        await conn.execute("""
            ALTER TABLE public.users
            ADD COLUMN IF NOT EXISTS training_program TEXT,
            ADD COLUMN IF NOT EXISTS training_form JSONB,
            ADD COLUMN IF NOT EXISTS reminder_enabled BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS reminder_time TIME,
            ADD COLUMN IF NOT EXISTS reminder_days TEXT[],
            ADD COLUMN IF NOT EXISTS reminder_duration TEXT,
            ADD COLUMN IF NOT EXISTS workout_duration INT
        """)

        # sets — добиваем недостающие поля
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS public.sets (
                set_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES public.users(user_id) ON DELETE CASCADE,
                photo BYTEA,
                verified BOOLEAN DEFAULT FALSE,
                gpt_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("ALTER TABLE public.sets ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE public.sets ADD COLUMN IF NOT EXISTS gpt_reason TEXT")
        await conn.execute("ALTER TABLE public.sets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

    @classmethod
    async def drop(cls):
        await cls.drop_all()

    @classmethod
    async def truncate(cls):
        await cls.truncate_all()
