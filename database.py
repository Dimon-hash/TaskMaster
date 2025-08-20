import logging
from typing import Optional

import asyncpg
from config import settings

logger = logging.getLogger(__name__)

# В рантайме работаем без пула — проще на Windows/Py3.12.
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
    Обёртка над БД.
      - init() — создаёт пул (или одноразовое подключение) и применяет схему/миграции
      - acquire() — возвращает контекст-менеджер соединения
      - truncate_all()/drop_all() — разрушительные операции
      - close() — закрыть пул (если создавали)
    """
    pool: Optional[asyncpg.Pool] = None  # тип пула из asyncpg

    @classmethod
    async def init(cls) -> None:
        """Инициализация БД и применение миграций."""
        dsn = str(settings.DATABASE_URL)

        if USE_POOL_FOR_RUNTIME:
            if cls.pool is not None:
                return
            cls.pool = await asyncpg.create_pool(dsn=dsn)
            async with cls.pool.acquire() as conn:
                await conn.execute("SET search_path TO public")
                await cls._ensure_schema(conn)
                await cls._run_migrations(conn)
                row = await conn.fetchrow(
                    "SELECT current_database() AS db, current_user AS usr, current_schema() AS sch"
                )
                logger.info("DB connected: db=%s user=%s schema=%s", row["db"], row["usr"], row["sch"])
        else:
            # Без пула: просто откроем соединение, применим схему/миграции и закроем.
            conn = await asyncpg.connect(dsn=dsn)
            try:
                await conn.execute("SET search_path TO public")
                await cls._ensure_schema(conn)
                await cls._run_migrations(conn)
                row = await conn.fetchrow(
                    "SELECT current_database() AS db, current_user AS usr, current_schema() AS sch"
                )
                logger.info("DB connected: db=%s user=%s schema=%s", row["db"], row["usr"], row["sch"])
            finally:
                await conn.close()

        logger.info("Database initialized successfully.")

    @classmethod
    def acquire(cls):
        """
        Возвращает контекст-менеджер соединения:
          - при USE_POOL_FOR_RUNTIME=True — из пула;
          - иначе — одноразовое соединение.
        """
        dsn = str(settings.DATABASE_URL)
        if USE_POOL_FOR_RUNTIME:
            if cls.pool is None:
                raise RuntimeError("Pool is not initialized. Call Database.init() first.")
            return cls.pool.acquire()
        return _DirectConn(dsn)

    @classmethod
    async def truncate_all(cls) -> None:
        """TRUNCATE всех таблиц с RESTART IDENTITY."""
        conn = await asyncpg.connect(dsn=str(settings.DATABASE_URL))
        try:
            await conn.execute("SET search_path TO public")
            await conn.execute("TRUNCATE TABLE public.sets RESTART IDENTITY CASCADE")
            await conn.execute("TRUNCATE TABLE public.tasks RESTART IDENTITY CASCADE")
            await conn.execute("TRUNCATE TABLE public.users RESTART IDENTITY CASCADE")
            logger.info("All tables truncated (users/tasks/sets).")
        finally:
            await conn.close()

    @classmethod
    async def drop_all(cls) -> None:
        """DROP всех таблиц (безопасно с IF EXISTS)."""
        conn = await asyncpg.connect(dsn=str(settings.DATABASE_URL))
        try:
            await conn.execute("SET search_path TO public")
            await conn.execute("DROP TABLE IF EXISTS public.sets CASCADE")
            await conn.execute("DROP TABLE IF EXISTS public.tasks CASCADE")
            await conn.execute("DROP TABLE IF EXISTS public.users CASCADE")
            logger.info("All tables dropped (users/tasks/sets).")
        finally:
            await conn.close()

    @classmethod
    async def close(cls) -> None:
        """Закрыть пул, если он был создан."""
        if cls.pool is not None:
            await cls.pool.close()
            cls.pool = None
            logger.info("Database connection pool closed.")

    # --------- Внутреннее: создание базовой схемы/таблиц ----------
    @classmethod
    async def _ensure_schema(cls, conn: asyncpg.Connection) -> None:
        """Базовые CREATE TABLE IF NOT EXISTS (безопасно)."""
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
                -- legacy
                reminder_duration TEXT,
                -- актуальная длительность в минутах (общая)
                workout_duration INT,
                -- отдых между подходами в секундах
                rest_seconds INT
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

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS public.tasks (
                task_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES public.users(user_id) ON DELETE CASCADE,
                title TEXT,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    @classmethod
    async def _run_migrations(cls, conn: asyncpg.Connection) -> None:
        """
        Идемпотентные миграции. Можно запускать многократно.
        """
        # users: гарантируем наличие нужных колонок
        await conn.execute("""
            ALTER TABLE public.users
            ADD COLUMN IF NOT EXISTS training_program TEXT,
            ADD COLUMN IF NOT EXISTS training_form JSONB,
            ADD COLUMN IF NOT EXISTS reminder_enabled BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS reminder_time TIME,
            ADD COLUMN IF NOT EXISTS reminder_days TEXT[],
            ADD COLUMN IF NOT EXISTS reminder_duration TEXT,
            ADD COLUMN IF NOT EXISTS workout_duration INT,
            ADD COLUMN IF NOT EXISTS rest_seconds INT
        """)

        # sets: гарантируем наличие нужных колонок
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

        # tasks: гарантируем наличие таблицы
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS public.tasks (
                task_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES public.users(user_id) ON DELETE CASCADE,
                title TEXT,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    # Совместимые алиасы — как у тебя в handlers.py
    @classmethod
    async def drop(cls):
        await cls.drop_all()

    @classmethod
    async def truncate(cls):
        await cls.truncate_all()
