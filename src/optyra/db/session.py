"""Engine/session factory, schema bootstrap, and the single-worker advisory lock."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


# Query params that SQLAlchemy passes straight through to asyncpg.connect(),
# which rejects them (verified empirically: `sslmode` -> TypeError,
# `channel_binding` -> startup crash). Managed providers (Neon, Aiven) put
# them in copy-pasted URLs, so strip them here — asyncpg negotiates TLS
# on its own (ssl=prefer default) and the connection stays encrypted.
_ASYNCPG_REJECTED_PARAMS = frozenset({"sslmode", "channel_binding"})


def _strip_rejected_query_params(database_url: str) -> str:
    split = urlsplit(database_url)
    if not split.query:
        return database_url
    kept = [
        (k, v) for k, v in parse_qsl(split.query, keep_blank_values=True) if k not in _ASYNCPG_REJECTED_PARAMS
    ]
    if len(kept) == len(parse_qsl(split.query, keep_blank_values=True)):
        return database_url
    stripped = urlunsplit(split._replace(query=urlencode(kept)))
    logger.info(
        "removed unsupported query param(s) from DATABASE_URL (%s); TLS is still negotiated by asyncpg",
        sorted(_ASYNCPG_REJECTED_PARAMS),
    )
    return stripped


def create_engine(database_url: str, *, pool_size: int = 5) -> AsyncEngine:
    """asyncpg URL: postgresql+asyncpg://user:pass@host:5432/dbname"""
    database_url = _strip_rejected_query_params(database_url)
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
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
        exists = await conn.execute(text("SELECT value FROM meta WHERE key = 'schema_version'"))
        row = exists.first()
        if row is None:
            await conn.execute(MetaInfo.__table__.insert().values(key="schema_version", value=SCHEMA_VERSION))
        elif row[0] != SCHEMA_VERSION:
            logger.warning(
                "database schema version %s differs from expected %s; continuing",
                row[0],
                SCHEMA_VERSION,
            )


class WorkerLock:
    """Postgres advisory lock so two worker processes can't run against one DB.

    On SQLite (tests/dev) this is a no-op that always succeeds.

    `acquire()` waits up to `timeout_seconds` for a lock held by a previous
    instance instead of failing instantly. That is what makes zero-downtime
    deploys work on PaaS hosts (Render free tier starts the new instance
    before the old one exits): the newcomer patiently outlives the overlap
    instead of crashing into the old worker's lock.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn = None
        self.held = False

    async def _try_once(self) -> bool:
        self._conn = await self._engine.connect()
        got = (
            await self._conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        ).scalar()
        self.held = bool(got)
        if not got:
            await self._conn.close()
            self._conn = None
        return self.held

    async def acquire(self, *, timeout_seconds: float = 180.0, retry_interval_seconds: float = 5.0) -> bool:
        if self._engine.dialect.name != "postgresql":
            self.held = True
            return True
        if timeout_seconds <= 0:
            return await self._try_once()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        attempt = 0
        while True:
            attempt += 1
            if await self._try_once():
                if attempt > 1:
                    logger.info("worker lock acquired after %d attempts (previous instance exited)", attempt)
                return True
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "worker lock still held after %.0fs; giving up (another worker is alive)",
                    timeout_seconds,
                )
                return False
            logger.info(
                "worker lock held by another instance (attempt %d); retrying for up to %.0fs",
                attempt,
                max(0.0, deadline - asyncio.get_running_loop().time()),
            )
            await asyncio.sleep(retry_interval_seconds)

    async def release(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            finally:
                await self._conn.close()
                self._conn = None
        self.held = False


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        async with session.begin():
            yield session
