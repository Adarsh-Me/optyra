"""GitHubClient behavior tests over httpx.MockTransport (retries, pagination, limits)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from optyra.github.client import GitHubClient, GitHubError, NotFound
from optyra.github.token_bucket import TokenBucket


def make_client(handler, *, bucket=None, **kw) -> GitHubClient:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = GitHubClient(
        "test-token",
        bucket=bucket,
        transport=httpx.MockTransport(handler),
        sleep=record_sleep,
        **kw,
    )
    client.recorded_sleeps = sleeps  # type: ignore[attr-defined]
    return client


def issue_payload(number: int, age_hours: int = 1) -> dict:
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_hours * 3600))
    return {
        "id": number,
        "number": number,
        "title": f"issue {number}",
        "state": "open",
        "created_at": created,
        "user": {"login": "a"},
        "labels": [],
        "assignees": [],
        "repository_url": "https://api.github.com/repos/apache/kafka",
        "html_url": f"https://github.com/apache/kafka/issues/{number}",
        "body": "x" * 200,
    }


async def test_link_header_pagination():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={"items": [issue_payload(1), issue_payload(2)]},
                headers={"Link": '<https://api.github.com/search/issues?page=2&per_page=100>; rel="next"'},
            )
        assert request.url.params["page"] == "2"
        return httpx.Response(200, json={"items": [issue_payload(3)]})

    client = make_client(handler)
    async with client:
        items = await client.search_issues("apache", since=datetime.now(UTC) - timedelta(hours=24))
    assert calls["n"] == 2
    assert [i["number"] for i in items] == [1, 2, 3]


async def test_early_stop_when_items_older_than_watermark():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"items": [issue_payload(1, age_hours=30)]},  # older than 24h window
            headers={"Link": '<https://api.github.com/search/issues?page=2>; rel="next"'},
        )

    client = make_client(handler)
    async with client:
        items = await client.search_issues(
            "apache",
            since=datetime.now(UTC) - timedelta(hours=24),
        )
    assert calls["n"] == 1  # second page never fetched
    assert items == [issue_payload(1, age_hours=30)]


async def test_5xx_retries_with_backoff():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502, json={"message": "bad gateway"})
        return httpx.Response(200, json={"items": [issue_payload(1)]})

    client = make_client(handler)
    async with client:
        items = await client.search_issues("apache", since=datetime.now(UTC) - timedelta(hours=1))
    assert calls["n"] == 3
    assert len(client.recorded_sleeps) == 2  # backoff sleeps happened
    assert items[0]["number"] == 1


async def test_403_retry_after_honored():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, json={}, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"items": []})

    client = make_client(handler)
    async with client:
        await client.search_issues("apache", since=datetime.now(UTC))
    assert client.recorded_sleeps == [7.0]


async def test_429_gives_up_as_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={}, headers={"Retry-After": "1"})

    client = make_client(handler)
    async with client:
        with pytest.raises(GitHubError):
            await client.search_issues("apache", since=datetime.now(UTC))


async def test_404_raises_not_found():
    client = make_client(lambda request: httpx.Response(404, json={"message": "Not Found"}))
    async with client:
        with pytest.raises(NotFound):
            await client.get_issue("apache/kafka", 1)


async def test_primary_rate_limit_sleeps_until_reset():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"items": []},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 60)},
        )

    client = make_client(handler)
    client.rate_remaining = 0
    client.rate_reset_epoch = time.time() + 60
    async with client:
        await client.search_issues("apache", since=datetime.now(UTC))
    assert client.recorded_sleeps and 50 <= client.recorded_sleeps[0] <= 61
    assert client.rate_remaining == 0  # updated from response headers


async def test_search_passes_through_token_bucket():
    acquired = {"n": 0}

    class RecordingBucket(TokenBucket):
        async def acquire(self) -> None:
            acquired["n"] += 1

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    client = make_client(handler, bucket=RecordingBucket(6000))
    async with client:
        await client.search_issues("apache", since=datetime.now(UTC))
        await client.get_issue("apache/kafka", 1)  # REST: bucket NOT used
    assert acquired["n"] == 1


async def test_follows_redirect_for_renamed_repo():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/apache/old-name":
            return httpx.Response(
                200,
                json={
                    "id": 5,
                    "full_name": "apache/new-name",
                    "owner": {"login": "apache"},
                    "stargazers_count": 10,
                    "language": "Py",
                    "archived": False,
                    "pushed_at": None,
                },
            )
        return httpx.Response(500)

    client = make_client(handler)
    async with client:
        repo = await client.get_repo("apache/old-name")
    assert repo["full_name"] == "apache/new-name"
