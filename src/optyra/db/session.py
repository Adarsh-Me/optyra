"""Engine/session factory, schema bootstrap, and the single-worker advisory lock."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from optyra.db.models import SCHEMA_VERSION, Base, MetaInfo

logger = logging.getLogger(__name__)

# Arbitrary app-specific lock id for pg_try_advisory_lock.
ADVISORY_LOCK_KEY = 0x4F50_5459  # "OPTY"


def create_engine(database_url: str, *, pool_size: int = 5) -> AsyncEngine:
    """asyncpg URL: postgresql+asyncpg://user:pass@host:5432/dbname"""
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    kwargs: dict = {"echo": False}
    if database_url.startswith("postgresql"):
        kwargs["pool_size"] = pool_size
        kwargs["max_overflow"] = 2
        kwargs["pool_pre_ping"] = True
    return create_async_engine(database_url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def ensure_schema(engine: AsyncEngine) -> None:
    """Idempotent bootstrap: create tables if missing and stamp the schema version.

    Keeps deployment a plain `docker compose up -d` (no migration step). Future column
    additions should migrate here or move to Alembic once the schema churns.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        exists = await conn.execute(
            text("SELECT value FROM meta WHERE key = 'schema_version'")
        )
        row = exists.first()
        if row is None:
            await conn.execute(
                MetaInfo.__table__.insert().values(key="schema_version", value=SCHEMA_VERSION)
            )
        elif row[0] != SCHEMA_VERSION:
            logger.warning(
                "database schema version %s differs from expected %s; continuing",
                row[0],
                SCHEMA_VERSION,
            )


class WorkerLock:
    """Postgres advisory lock so two worker processes can't run against one DB.

    On SQLite (tests/dev) this is a no-op that always succeeds.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn = None
        self.held = False

    async def acquire(self) -> bool:
        if self._engine.dialect.name != "postgresql":
            self.held = True
            return True
        self._conn = await self._engine.connect()
        got = (
            await self._conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        ).scalar()
        self.held = bool(got)
        if not got:
            await self._conn.close()
            self._conn = None
        return self.held

    async def release(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY}
                )
            finally:
                await self._conn.close()
                self._conn = None
        self.held = False


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        async with session.begin():
            yield session
