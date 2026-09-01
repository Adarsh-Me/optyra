"""Shared fixtures: real config, sqlite DB, fake GitHub / AI / Telegram over MockTransport."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from optyra.ai.enricher import IssueEnricher
from optyra.config import AppConfig, load_config
from optyra.db.session import create_engine, create_session_factory, ensure_schema
from optyra.github.client import GitHubClient
from optyra.health import HealthState
from optyra.notify.telegram import TelegramNotifier
from optyra.services import Services

REPO_ROOT = Path(__file__).resolve().parents[1]


def utcnow() -> datetime:
    return datetime.now(UTC)


def gh_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ config


@pytest.fixture
def cfg(monkeypatch) -> AppConfig:
    monkeypatch.setenv("GH_TOKEN", "test-gh-token-1234567890abcdef")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:fake-token-abcdef")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("AI_API_KEY", "test-ai-key")
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return load_config(REPO_ROOT / "config")


def make_gh_token(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "test-gh-token-1234567890abcdef")


# ------------------------------------------------------------------ database


@pytest.fixture
async def db_factory(tmp_path):
    """SQLite per-test by default; real Postgres when TEST_DATABASE_URL is set (CI).

    The Postgres database is shared across tests, so it is truncated between them.
    """
    import os

    from sqlalchemy import text

    url = os.environ.get("TEST_DATABASE_URL")
    engine = create_engine(url) if url else create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await ensure_schema(engine)
    if url:
        async with engine.begin() as conn:
            for table in ("issues", "notifications", "repos", "orgs", "poll_state", "meta"):
                await conn.execute(text(f"DELETE FROM {table}"))
    yield create_session_factory(engine)
    await engine.dispose()


# ------------------------------------------------------------------ fakes


def make_issue_item(
    repo: str = "apache/kafka",
    number: int = 1,
    *,
    title: str = "NullPointerException in stream rebalance",
    body: str | None = None,
    labels: tuple[str, ...] = ("good first issue",),
    assignees: tuple[str, ...] = (),
    author: str = "alice",
    created_min_ago: int = 5,
) -> dict:
    if body is None:
        body = (
            "When running the connector with a custom partitioner we hit a NPE.\n"
            "Repro steps:\n1. start broker\n2. submit connector config\n3. observe crash\n"
            "```\njava.lang.NullPointerException at KafkaBroker.scala:120\n```\n" + "Expected: no crash. " * 4
        )
    return {
        "id": 100000 + number,
        "number": number,
        "title": title,
        "state": "open",
        "created_at": gh_time(utcnow() - timedelta(minutes=created_min_ago)),
        "user": {"login": author},
        "labels": [{"name": label} for label in labels],
        "assignees": [{"login": a} for a in assignees],
        "repository_url": f"https://api.github.com/repos/{repo}",
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "body": body,
    }


def issue_values(number: int = 1, **overrides) -> dict:
    """Fully-parsed issue ready for DAL.insert_issue."""
    from optyra.core.normalize import parse_search_item

    parsed = parse_search_item(make_issue_item(number=number))
    values = {
        "repo_full_name": parsed.repo_full_name,
        "number": parsed.number,
        "title": parsed.title,
        "state": parsed.state,
        "author": parsed.author,
        "created_at": parsed.created_at,
        "labels": parsed.labels,
        "assignees": parsed.assignees,
        "raw": parsed.raw,
    }
    values.update(overrides)
    return values


def make_repo_item(full_name: str = "apache/kafka", *, stars: int = 28000) -> dict:
    owner = full_name.split("/")[0]
    return {
        "id": abs(hash(full_name)) % 1000000,
        "full_name": full_name,
        "owner": {"login": owner},
        "stargazers_count": stars,
        "language": "Python",
        "archived": False,
        "pushed_at": gh_time(utcnow() - timedelta(days=1)),
    }


class FakeGitHub:
    """Routes GitHub API requests from GitHubClient through an in-memory world."""

    def __init__(self) -> None:
        self.search_issues_items: dict[str, list[dict]] = {}
        self.search_repo_items: dict[str, list[dict]] = {}
        self.issues: dict[tuple[str, int], dict] = {}
        self.timelines: dict[tuple[str, int], list[dict]] = {}
        self.calls: list[str] = []
        self.fail_next_n: dict[str, int] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _json(self, payload: dict, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(200, json=payload, headers=headers or {})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = httpx.URL(str(request.url))
        self.calls.append(f"{request.method} {url.path}?{url.params.get('q', '')}")
        path = url.path

        if path == "/search/issues":
            query = url.params.get("q", "")
            org = query.split()[0].removeprefix("org:")
            items = self.search_issues_items.get(org, [])
            return self._json({"total_count": len(items), "items": items})

        if path == "/search/repositories":
            query = url.params.get("q", "")
            org = query.split()[0].removeprefix("org:")
            items = self.search_repo_items.get(org, [])
            return self._json({"total_count": len(items), "items": items})

        parts = path.strip("/").split("/")
        if len(parts) == 6 and parts[0] == "repos" and parts[3] == "issues" and parts[5] == "timeline":
            key = (f"{parts[1]}/{parts[2]}", int(parts[4]))
            return self._json(self.timelines.get(key, []))
        if len(parts) == 5 and parts[0] == "repos" and parts[3] == "issues":
            key = (f"{parts[1]}/{parts[2]}", int(parts[4]))
            issue = self.issues.get(key)
            if issue is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return self._json(issue)
        if len(parts) == 3 and parts[0] == "repos":
            full_name = f"{parts[1]}/{parts[2]}"
            for items in self.search_repo_items.values():
                for item in items:
                    if item["full_name"] == full_name:
                        return self._json(item)
            return httpx.Response(404, json={"message": "Not Found"})

        return httpx.Response(500, json={"message": f"unrouted: {path}"})


class FakeAI:
    """Queue of raw generateContent payloads / status codes."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        item = self.responses.pop(0) if self.responses else {"status": 500}
        if isinstance(item, int):
            return httpx.Response(item, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=item)


def ai_response(payload: dict) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}


class FakeTelegram:
    def __init__(self, *, status_sequence: list[int] | None = None) -> None:
        self.status_sequence = list(status_sequence or [])
        self.sent: list[dict] = []
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            self.sent.append(json.loads(request.content.decode()))
        status = self.status_sequence.pop(0) if self.status_sequence else 200
        if status == 429:
            return httpx.Response(429, json={"parameters": {"retry_after": 3}, "ok": False})
        return httpx.Response(status, json={"ok": status == 200})


# ------------------------------------------------------------------ services builder


def make_services(
    cfg: AppConfig,
    db_factory,
    gh: FakeGitHub,
    *,
    tg: FakeTelegram | None = None,
    ai: FakeAI | None = None,
) -> tuple[Services, GitHubClient, TelegramNotifier | None, IssueEnricher | None]:
    gh_client = GitHubClient(cfg.secrets.gh_token, bucket=None, transport=gh.transport(), rest_concurrency=8)
    tg_notifier = None
    if tg is not None:
        tg_notifier = TelegramNotifier(
            cfg.secrets.telegram_bot_token,
            list(cfg.secrets.telegram_chat_ids),
            api_base=cfg.telegram.api_base,
            parse_mode=cfg.telegram.parse_mode,
            transport=tg.transport(),
            sleep=tg.sleep,
        )
    enricher = None
    if ai is not None:
        enricher = IssueEnricher(
            api_key=cfg.secrets.ai_api_key,
            model=cfg.ai.model,
            criteria=cfg.ai_criteria,
            timeout_seconds=cfg.ai.timeout_seconds,
            max_retries=cfg.ai.max_retries,
            max_body_chars=cfg.ai.max_body_chars,
            summary_max_chars=cfg.ai.summary_max_chars,
            transport=ai.transport(),
        )
    services = Services(
        cfg=cfg,
        session_factory=db_factory,
        gh=gh_client,
        health=HealthState(),
        tg=tg_notifier,
        enricher=enricher,
    )
    return services, gh_client, tg_notifier, enricher
