"""GitHub REST/Search client (report 01prd §3, §16).

- Fine-grained PAT auth, redirects followed (renamed repos return 301 — we follow).
- 5xx: exponential backoff + jitter. 403/429: honor Retry-After / X-RateLimit-Reset.
- X-RateLimit-Remaining == 0: sleep until reset before the request (proactive).
- /search/* requests pass through the shared token bucket (<= ~20 req/min sustained).
- Link-header pagination with page caps; early stop on search/issues by watermark.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from optyra.github.token_bucket import TokenBucket

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
USER_AGENT = "optyra-gsoc-monitor"
API_VERSION = "2022-11-28"

_MAX_BACKOFF_SECONDS = 900.0
_DEFAULT_RETRIES = 3


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class NotFound(GitHubError):
    def __init__(self, message: str, status: int = 404) -> None:
        super().__init__(message, status)


class RateLimited(GitHubError):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_gh_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        bucket: TokenBucket | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        rest_concurrency: int = 5,
        retries: int = _DEFAULT_RETRIES,
        sleep: Callable[[float], Any] = asyncio.sleep,
        base_url: str = API_BASE,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            follow_redirects=True,  # renamed repos: 301 -> follow, report §16
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )
        self.bucket = bucket
        self.retries = retries
        self._sleep = sleep
        self._rest_semaphore = asyncio.Semaphore(rest_concurrency)
        # Last observed rate-limit state (for proactive waiting + /healthz).
        self.rate_remaining: int | None = None
        self.rate_reset_epoch: float | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core request

    async def _get(
        self,
        url: str,
        *,
        params: dict | None = None,
        is_search: bool = False,
        attempt: int = 0,
    ) -> httpx.Response:
        if is_search and self.bucket is not None:
            await self.bucket.acquire()

        if self.rate_remaining == 0 and self.rate_reset_epoch:
            wait = min(self.rate_reset_epoch - time.time(), _MAX_BACKOFF_SECONDS)
            if wait > 0:
                logger.warning("GitHub primary rate limit exhausted; sleeping %.0fs", wait)
                await self._sleep(wait)

        response = await self._client.get(url, params=params)

        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            self.rate_remaining = int(remaining)
        if reset is not None:
            self.rate_reset_epoch = float(reset)

        if response.is_success:
            return response

        status = response.status_code
        if status in (403, 429):
            retry_after = response.headers.get("Retry-After")
            wait = _MAX_BACKOFF_SECONDS
            if retry_after and retry_after.isdigit():
                wait = float(retry_after)
            elif reset:
                wait = max(0.0, float(reset) - time.time())
            else:
                wait = 60.0
            wait = min(wait, _MAX_BACKOFF_SECONDS)
            if attempt < self.retries:
                logger.warning("GitHub %s; backing off %.0fs (attempt %s)", status, wait, attempt + 1)
                await self._sleep(wait)
                return await self._get(url, params=params, is_search=is_search, attempt=attempt + 1)
            raise RateLimited(f"rate limited (HTTP {status}) after retries: {response.text[:200]}", status)

        if status >= 500 and attempt < self.retries:
            backoff = min((2**attempt) + random.uniform(0, 1), 30.0)
            logger.warning("GitHub %s; retrying in %.1fs", status, backoff)
            await self._sleep(backoff)
            return await self._get(url, params=params, is_search=is_search, attempt=attempt + 1)

        if status in (404, 410):
            raise NotFound(f"{url} -> {status}", status)
        raise GitHubError(f"GitHub {status} for {url}: {response.text[:200]}", status)

    async def _paginate(
        self,
        url: str,
        *,
        params: dict,
        is_search: bool,
        max_pages: int,
        stop_before: datetime | None = None,
        item_key: str = "items",
    ) -> list[dict]:
        """Follow Link headers. If stop_before is set, stop once items get older than it
        (search results are sorted desc by created)."""
        items: list[dict] = []
        page_url: str | None = url
        page_params = params
        for _ in range(max_pages):
            response = await self._get(page_url, params=page_params, is_search=is_search)
            payload = response.json()
            page_items = payload.get(item_key, []) if isinstance(payload, dict) else payload
            items.extend(page_items)
            if stop_before is not None and page_items:
                oldest = _parse_gh_time(page_items[-1].get("created_at"))
                if oldest is not None and oldest < stop_before:
                    break
            page_url = response.links.get("next", {}).get("url")
            page_params = None
            if not page_url:
                break
        return items

    # ------------------------------------------------------------------ endpoints

    async def search_repositories(
        self, org: str, *, min_stars: int, per_page: int = 100, max_pages: int = 10
    ) -> list[dict]:
        params = {
            "q": f"org:{org} stars:>={min_stars} archived:false",
            "sort": "stars",
            "order": "desc",
            "per_page": per_page,
        }
        # /search/repositories has no created_at; no watermark early-stop here.
        return await self._paginate(
            "/search/repositories",
            params=params,
            is_search=True,
            max_pages=max_pages,
            item_key="items",
        )

    async def search_issues(
        self,
        org: str,
        *,
        since: datetime,
        until: datetime | None = None,
        per_page: int = 100,
        max_pages: int = 10,
    ) -> list[dict]:
        """New issues in the org since `since` (exclusive-safe: caller passes overlap)."""
        created = f"created:>={_iso(since)}"
        if until is not None:
            created = f"created:{_iso(since)}..{_iso(until)}"
        params = {
            "q": f"org:{org} is:issue is:open {created}",
            "sort": "created",
            "order": "desc",
            "per_page": per_page,
        }
        return await self._paginate(
            "/search/issues",
            params=params,
            is_search=True,
            max_pages=max_pages,
            stop_before=since,
            item_key="items",
        )

    async def get_issue(self, repo_full_name: str, number: int) -> dict:
        async with self._rest_semaphore:
            response = await self._get(f"/repos/{repo_full_name}/issues/{number}")
        return response.json()

    async def get_issue_timeline(
        self, repo_full_name: str, number: int, *, max_pages: int = 3, per_page: int = 100
    ) -> list[dict]:
        events: list[dict] = []
        page_url: str | None = f"/repos/{repo_full_name}/issues/{number}/timeline"
        params: dict | None = {"per_page": per_page}
        for _ in range(max_pages):
            async with self._rest_semaphore:
                response = await self._get(page_url, params=params)
            batch = response.json()
            if not isinstance(batch, list):
                break
            events.extend(batch)
            page_url = response.links.get("next", {}).get("url")
            params = None
            if not page_url:
                break
        return events

    async def get_repo(self, repo_full_name: str) -> dict:
        async with self._rest_semaphore:
            response = await self._get(f"/repos/{repo_full_name}")
        return response.json()
