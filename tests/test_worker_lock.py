"""WorkerLock + Render-compat config tests.

- Free acquire works on every dialect (SQLite lock is a no-op).
- Contention behavior (wait-then-timeout, handoff on release) is exercised
  only on real PostgreSQL — SQLite has no advisory locks.
- URL sanitizing, $PORT override, and lock-timeout defaults are pure unit tests.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from optyra.config import load_config
from optyra.db.session import WorkerLock, create_engine

REPO_ROOT = Path(__file__).resolve().parents[1]

NEEDS_PG = os.environ.get("TEST_DATABASE_URL") is None
PG_URL = os.environ.get("TEST_DATABASE_URL")


def _pg_engine():
    assert PG_URL, "TEST_DATABASE_URL required"
    url = PG_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_engine(url)


async def test_lock_free_acquire_sqlite(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/lock.db")
    try:
        lock = WorkerLock(engine)
        assert await lock.acquire(timeout_seconds=5) is True
        assert lock.held is True
        await lock.release()
        assert lock.held is False
    finally:
        await engine.dispose()


@pytest.mark.skipif(NEEDS_PG, reason="advisory locks need real PostgreSQL")
async def test_lock_free_acquire_postgres():
    engine = _pg_engine()
    try:
        lock = WorkerLock(engine)
        assert await lock.acquire(timeout_seconds=5) is True
        await lock.release()
    finally:
        await engine.dispose()


@pytest.mark.skipif(NEEDS_PG, reason="advisory lock contention needs real PostgreSQL")
async def test_lock_contention_waits_then_times_out():
    first = _pg_engine()
    second = _pg_engine()
    holder = WorkerLock(first)
    try:
        assert await holder.acquire(timeout_seconds=5) is True
        waiter = WorkerLock(second)
        started = time.monotonic()
        assert await waiter.acquire(timeout_seconds=2, retry_interval_seconds=0.2) is False
        assert time.monotonic() - started >= 1.5
    finally:
        await holder.release()
        await first.dispose()
        await second.dispose()


@pytest.mark.skipif(NEEDS_PG, reason="advisory lock handoff needs real PostgreSQL")
async def test_lock_handoff_on_release():
    """Render overlap scenario: newcomer waits, old instance exits, newcomer proceeds."""
    first = _pg_engine()
    second = _pg_engine()
    try:
        old = WorkerLock(first)
        assert await old.acquire(timeout_seconds=5) is True
        new = WorkerLock(second)
        assert await new.acquire(timeout_seconds=1, retry_interval_seconds=0.2) is False
        await old.release()  # old instance got SIGTERM and shut down cleanly
        assert await new.acquire(timeout_seconds=10, retry_interval_seconds=0.2) is True
        await new.release()
    finally:
        await first.dispose()
        await second.dispose()


def test_create_engine_strips_rejected_query_params():
    engine = create_engine(
        "postgresql://user:pass@host:5432/db?sslmode=require&channel_binding=require&connect_timeout=10"
    )
    rendered = str(engine.url)
    assert "sslmode" not in rendered
    assert "channel_binding" not in rendered
    assert "connect_timeout=10" in rendered  # unknown-but-harmless params are kept


def test_create_engine_plain_url_untouched():
    engine = create_engine("postgresql://user:pass@host:5432/db")
    # str() masks the password; render_as_string reveals the untouched URL
    assert engine.url.render_as_string(hide_password=False) == "postgresql+asyncpg://user:pass@host:5432/db"


def test_port_env_overrides_healthz_port(monkeypatch, cfg):
    monkeypatch.setenv("PORT", "10000")
    assert load_config(REPO_ROOT / "config").ops.healthz_port == 10000


def test_invalid_port_falls_back_to_yaml(monkeypatch, cfg):
    monkeypatch.setenv("PORT", "not-a-port")
    assert load_config(REPO_ROOT / "config").ops.healthz_port == 8080


def test_worker_lock_timeout_default(monkeypatch, cfg):
    monkeypatch.delenv("PORT", raising=False)
    fresh = load_config(REPO_ROOT / "config")
    assert fresh.ops.worker_lock_timeout_seconds == 180
    assert fresh.ops.healthz_port == 8080  # no $PORT in test env
