"""Job B — the tiered issue poller (report 01prd §7-8, 02prd §3).

Every sweep: find orgs whose tier interval elapsed (circuit-breaker aware), search
`org:{o} is:issue is:open created:>=watermark-overlap` per org through the shared token
bucket, dedupe on the composite PK, whitelist against monitored repos, hard-filter,
score, deep-check candidates, enrich, notify (instant / digest).

Watermark semantics: on success watermark := window end; overlap gives at-least-once
detection while the DB PK gives exactly-once notification. If the watermark is older
than max_catchup_hours, polls run in time-sliced catch-up windows instead.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from optyra.core.normalize import parse_search_item
from optyra.db.dal import DAL, utcnow_aware
from optyra.db.models import Org, Repo
from optyra.jobs.candidate import CandidatePipeline
from optyra.services import Services

logger = logging.getLogger(__name__)

SWEEP_TICK_SECONDS = 10.0


@dataclass
class SweepStats:
    orgs_due: int = 0
    polled: int = 0
    new_issues: int = 0
    candidates: int = 0
    instant: int = 0
    digest: int = 0
    errors: int = 0
    skipped_not_whitelisted: int = 0
    details: list[str] = field(default_factory=list)


class IssuePoller:
    def __init__(self, services: Services) -> None:
        self.svc = services
        self.cfg = services.cfg
        self.pipeline = CandidatePipeline(services)
        self._next_due: dict[str, datetime] = {}
        self._started = False

    # ------------------------------------------------------------------ loop

    async def run_forever(self) -> None:
        while True:
            started = utcnow_aware()
            try:
                stats = await self.sweep()
                self._update_health(stats)
                if stats.polled:
                    logger.info(
                        "sweep: due=%s polled=%s new=%s candidates=%s instant=%s digest=%s "
                        "errors=%s skipped=%s",
                        stats.orgs_due,
                        stats.polled,
                        stats.new_issues,
                        stats.candidates,
                        stats.instant,
                        stats.digest,
                        stats.errors,
                        stats.skipped_not_whitelisted,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("poll sweep crashed; continuing")
            elapsed = (utcnow_aware() - started).total_seconds()
            await asyncio.sleep(max(1.0, SWEEP_TICK_SECONDS - elapsed))

    def _update_health(self, stats: SweepStats) -> None:
        health = self.svc.health
        health.last_sweep_at = utcnow_aware().isoformat(timespec="seconds")
        health.last_sweep_polled = stats.polled
        health.last_sweep_new_issues = stats.new_issues
        health.last_sweep_errors = stats.errors
        health.last_sweep_instant = stats.instant
        health.last_sweep_digest = stats.digest
        health.github_rate_remaining = self.svc.gh.rate_remaining

    # ------------------------------------------------------------------ sweep

    async def sweep(self) -> SweepStats:
        stats = SweepStats()
        now = utcnow_aware()
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                orgs = await dal.get_orgs()
        due = [org for org in orgs if self._is_due(org, now)]
        stats.orgs_due = len(due)
        if not due:
            return stats

        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                repo_rows_list = await dal.monitored_repo_rows()
        repo_rows = dict(repo_rows_list)

        semaphore = asyncio.Semaphore(self.cfg.poll.concurrency)

        async def run_one(org: Org) -> None:
            async with semaphore:
                org_stats = await self._poll_org(org, now, repo_rows)
            for key in (
                "polled",
                "new_issues",
                "candidates",
                "instant",
                "digest",
                "errors",
                "skipped_not_whitelisted",
            ):
                setattr(stats, key, getattr(stats, key) + getattr(org_stats, key))
            if org_stats.details:
                stats.details.extend(org_stats.details)

        results = await asyncio.gather(*(run_one(org) for org in due), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                stats.errors += 1
                logger.error("org poll task failed: %r", result)
        return stats

    def _is_due(self, org: Org, now: datetime) -> bool:
        interval = (
            self.cfg.poll.tier1_interval_seconds if org.tier == 1 else self.cfg.poll.tier2_interval_seconds
        )
        next_due = self._next_due.get(org.login)
        if next_due is None:
            # stagger fresh orgs across their interval to smooth startup load
            index = len(self._next_due)
            self._next_due[org.login] = now + timedelta(seconds=index * interval / 60)
            return False
        return now >= next_due

    # ------------------------------------------------------------------ per-org poll

    async def _poll_org(self, org: Org, now: datetime, repo_rows: dict[str, Repo]) -> SweepStats:
        stats = SweepStats()
        interval = (
            self.cfg.poll.tier1_interval_seconds if org.tier == 1 else self.cfg.poll.tier2_interval_seconds
        )
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                state = await dal.get_poll_state(org.login)
                if state is None:
                    watermark = now - timedelta(hours=self.cfg.poll.max_backfill_hours)
                    state = await dal.init_poll_state(org.login, watermark)
                watermark = state.watermark
                if watermark.tzinfo is None:
                    watermark = watermark.replace(tzinfo=UTC)
                breaker_until = state.breaker_until
                if breaker_until is not None and breaker_until.tzinfo is None:
                    breaker_until = breaker_until.replace(tzinfo=UTC)
                if breaker_until is not None and breaker_until > now:
                    self._schedule_next(org.login, now, interval)
                    return stats
                stats.polled = 1  # this org's search actually ran
                windows, catchup = self._compute_windows(watermark, now)
                gsoc_score = await dal.org_gsoc_score(org.login)
                try:
                    for start, end in windows:
                        items = await self.svc.gh.search_issues(
                            org.login,
                            since=start - timedelta(seconds=self.cfg.poll.overlap_seconds),
                            until=end,
                            per_page=self.cfg.poll.page_size,
                        )
                        stats.new_issues += await self._process(
                            dal, org, items, now, repo_rows, gsoc_score, stats
                        )
                        await dal.update_poll_state(
                            org.login,
                            watermark=end,
                            ok=True,
                            breaker_failures=self.cfg.poll.breaker_failures,
                            breaker_cooldown_seconds=self.cfg.poll.breaker_cooldown_seconds,
                        )
                except Exception as exc:
                    await dal.update_poll_state(
                        org.login,
                        ok=False,
                        breaker_failures=self.cfg.poll.breaker_failures,
                        breaker_cooldown_seconds=self.cfg.poll.breaker_cooldown_seconds,
                    )
                    stats.errors += 1
                    stats.details.append(f"{org.login}: {exc!r}")
                    self._schedule_next(org.login, now, interval)
                    return stats
        # success: schedule next per tier; keep catching up quickly if still behind
        next_interval = interval
        if catchup:
            async with self.svc.session_factory() as session:
                async with session.begin():
                    dal = DAL(session)
                    state = await dal.get_poll_state(org.login)
                    if state is not None:
                        wm = state.watermark
                        if wm.tzinfo is None:
                            wm = wm.replace(tzinfo=UTC)
                        if wm < now:
                            next_interval = min(next_interval, SWEEP_TICK_SECONDS * 2)
        self._schedule_next(org.login, utcnow_aware(), next_interval)
        return stats

    def _schedule_next(self, login: str, now: datetime, interval: float) -> None:
        self._next_due[login] = now + timedelta(seconds=interval)

    def _compute_windows(
        self, watermark: datetime, now: datetime
    ) -> tuple[list[tuple[datetime, datetime]], bool]:
        max_catchup = now - timedelta(hours=self.cfg.poll.max_catchup_hours)
        if watermark >= max_catchup:
            return [(watermark, now)], False
        # catch-up: time-sliced windows so no single query overflows 1000 results
        windows = []
        cursor = watermark
        for _ in range(8):
            end = min(cursor + timedelta(seconds=self.cfg.poll.catchup_window_seconds), now)
            if end <= cursor:
                break
            windows.append((cursor, end))
            cursor = end
            if cursor >= now:
                break
        logger.info("catch-up mode: %s window(s) from watermark %s", len(windows), watermark)
        return windows, True

    # ------------------------------------------------------------------ item processing

    async def _process(
        self,
        dal: DAL,
        org: Org,
        items: list[dict],
        now: datetime,
        repo_rows: dict[str, Repo],
        gsoc_score: int,
        stats: SweepStats,
    ) -> int:
        """Dedupe-first: insert-if-new, then filter/score only brand-new rows."""
        new_count = 0
        for item in items:
            parsed = parse_search_item(item)
            if parsed is None:
                continue
            repo_row = repo_rows.get(parsed.repo_full_name.lower())
            if repo_row is None:
                stats.skipped_not_whitelisted += 1
                continue
            inserted = await dal.insert_issue(
                {
                    "repo_full_name": parsed.repo_full_name,
                    "number": parsed.number,
                    "title": parsed.title,
                    "state": parsed.state,
                    "author": parsed.author,
                    "created_at": parsed.created_at,
                    "labels": parsed.labels,
                    "assignees": parsed.assignees,
                    "raw": parsed.raw,
                    "score": 0,
                    "gsoc_score": gsoc_score,
                }
            )
            if not inserted:
                continue
            new_count += 1
            outcome = await self.pipeline.process_new_issue(dal, parsed, repo_row=repo_row, now=now)
            if outcome.score >= self.cfg.notify.digest_threshold:
                stats.candidates += 1
            if outcome.notified_instant:
                stats.instant += 1
            if outcome.queued_digest:
                stats.digest += 1
        return new_count
