"""Job B — the v2 poller: one tier, every scope (16 orgs + 1 combined pinned query).

Every sweep: for scopes whose interval elapsed (breaker-aware), search
`org:{o} is:issue is:open created:>=watermark-overlap` (or `repo:a repo:b repo:c ...`
for the pinned scope) through the shared token bucket, dedupe on the composite PK,
apply the star gate CLIENT-SIDE (GitHub's search cannot express `stars:` for issue
queries — verified live), hard-filter, score, deep-check candidates, enrich,
setup-policy, notify (instant / digest), and record funnel counters.

Watermark semantics unchanged from v1: on success watermark := window end; overlap gives
at-least-once detection while the DB PK gives exactly-once notification. Catch-up mode
slices time when a scope is >max_catchup_hours behind.

On-demand repo gate: an issue from a repo not seen by discovery is resolved with one
GET /repos call (cached forever): org member + stars >= min_stars (or pinned) ->
monitored, else dropped. That removes the v1 race where a repo crossing the threshold
between nightly syncs lost issues for up to 24 h.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from optyra.core.normalize import parse_repo_item, parse_search_item
from optyra.db.dal import DAL, utcnow_aware
from optyra.db.models import Repo
from optyra.github.client import NotFound
from optyra.jobs.candidate import CandidatePipeline
from optyra.services import Services

logger = logging.getLogger(__name__)

SWEEP_TICK_SECONDS = 10.0
PINNED_SCOPE = "pinned"


@dataclass
class SweepStats:
    scopes_due: int = 0
    polled: int = 0
    new_issues: int = 0
    instant: int = 0
    digest: int = 0
    errors: int = 0
    gate_rejected: int = 0
    details: list[str] = field(default_factory=list)


class IssuePoller:
    def __init__(self, services: Services) -> None:
        self.svc = services
        self.cfg = services.cfg
        self.pipeline = CandidatePipeline(services)
        self._next_due: dict[str, datetime] = {}

    # ------------------------------------------------------------------ loop

    async def run_forever(self) -> None:
        while True:
            started = utcnow_aware()
            try:
                stats = await self.sweep()
                self._update_health(stats)
                if stats.polled:
                    logger.info(
                        "sweep: due=%s polled=%s new=%s instant=%s digest=%s errors=%s gate_rejected=%s",
                        stats.scopes_due,
                        stats.polled,
                        stats.new_issues,
                        stats.instant,
                        stats.digest,
                        stats.errors,
                        stats.gate_rejected,
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

    def _scopes(self) -> list[tuple[str, dict]]:
        """Poll units: one per watched org login + the combined pinned-repo scope."""
        scopes: list[tuple[str, dict]] = [(login, {"org": login}) for login in self.cfg.watch.orgs]
        if self.cfg.watch.pinned_repos:
            scopes.append((PINNED_SCOPE, {"repos": list(self.cfg.watch.pinned_repos)}))
        return scopes

    async def sweep(self) -> SweepStats:
        stats = SweepStats()
        now = utcnow_aware()
        due = []
        for scope, spec in self._scopes():
            if self._is_due(scope, now):
                due.append((scope, spec))
        stats.scopes_due = len(due)
        if not due:
            return stats

        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                repo_rows = await dal.monitored_repo_rows()
        repo_rows = dict(repo_rows)

        semaphore = asyncio.Semaphore(self.cfg.poll.concurrency)

        async def run_one(scope: str, spec: dict) -> None:
            async with semaphore:
                scope_stats = await self._poll_scope(scope, spec, now, repo_rows)
            for key in ("polled", "new_issues", "instant", "digest", "errors", "gate_rejected"):
                setattr(stats, key, getattr(stats, key) + getattr(scope_stats, key))
            stats.details.extend(scope_stats.details)

        results = await asyncio.gather(*(run_one(s, p) for s, p in due), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                stats.errors += 1
                logger.error("scope poll task failed: %r", result)
        return stats

    def _is_due(self, scope: str, now: datetime) -> bool:
        next_due = self._next_due.get(scope)
        if next_due is None:
            index = len(self._next_due)
            self._next_due[scope] = now + timedelta(seconds=index * self.cfg.poll.interval_seconds / 60)
            return False
        return now >= next_due

    # ------------------------------------------------------------------ per-scope poll

    async def _poll_scope(
        self, scope: str, spec: dict, now: datetime, repo_rows: dict[str, Repo]
    ) -> SweepStats:
        stats = SweepStats()
        interval = self.cfg.poll.interval_seconds
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                state = await dal.get_poll_state(scope)
                if state is None:
                    watermark = now - timedelta(hours=self.cfg.poll.max_backfill_hours)
                    state = await dal.init_poll_state(scope, watermark)
                watermark = state.watermark
                if watermark.tzinfo is None:
                    watermark = watermark.replace(tzinfo=UTC)
                breaker_until = state.breaker_until
                if breaker_until is not None and breaker_until.tzinfo is None:
                    breaker_until = breaker_until.replace(tzinfo=UTC)
                if breaker_until is not None and breaker_until > now:
                    self._schedule_next(scope, now, interval)
                    return stats
                stats.polled = 1  # this scope's search actually ran
                windows, catchup = self._compute_windows(watermark, now)
                try:
                    for start, end in windows:
                        since = start - timedelta(seconds=self.cfg.poll.overlap_seconds)
                        if spec.get("repos"):
                            items = await self.svc.gh.search_issues_repos(
                                spec["repos"], since=since, until=end, per_page=self.cfg.poll.page_size
                            )
                        else:
                            items = await self.svc.gh.search_issues(
                                spec["org"], since=since, until=end, per_page=self.cfg.poll.page_size
                            )
                        stats.new_issues += await self._process(dal, items, now, repo_rows, stats)
                        await dal.update_poll_state(
                            scope,
                            watermark=end,
                            ok=True,
                            breaker_failures=self.cfg.poll.breaker_failures,
                            breaker_cooldown_seconds=self.cfg.poll.breaker_cooldown_seconds,
                        )
                except Exception as exc:
                    await dal.update_poll_state(
                        scope,
                        ok=False,
                        breaker_failures=self.cfg.poll.breaker_failures,
                        breaker_cooldown_seconds=self.cfg.poll.breaker_cooldown_seconds,
                    )
                    stats.errors += 1
                    stats.details.append(f"{scope}: {exc!r}")
                    self._schedule_next(scope, now, interval)
                    return stats
        # success: schedule next; keep catching up quickly if still behind
        next_interval = interval
        if catchup:
            async with self.svc.session_factory() as session:
                async with session.begin():
                    dal = DAL(session)
                    state = await dal.get_poll_state(scope)
                    if state is not None:
                        wm = state.watermark
                        if wm.tzinfo is None:
                            wm = wm.replace(tzinfo=UTC)
                        if wm < now:
                            next_interval = min(next_interval, SWEEP_TICK_SECONDS * 2)
        self._schedule_next(scope, utcnow_aware(), next_interval)
        return stats

    def _schedule_next(self, scope: str, now: datetime, interval: float) -> None:
        self._next_due[scope] = now + timedelta(seconds=interval)

    def _compute_windows(
        self, watermark: datetime, now: datetime
    ) -> tuple[list[tuple[datetime, datetime]], bool]:
        max_catchup = now - timedelta(hours=self.cfg.poll.max_catchup_hours)
        if watermark >= max_catchup:
            return [(watermark, now)], False
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
        items: list[dict],
        now: datetime,
        repo_rows: dict[str, Repo],
        stats: SweepStats,
    ) -> int:
        """Dedupe-first: insert-if-new, then filter/score only brand-new rows.

        v2 star gate: repo membership checked client-side (search can't express stars:
        for issues); unknown repos get one on-demand GET /repos call, cached forever.
        """
        new_count = 0
        blocked = self.cfg.watch.blocked_owners
        for item in items:
            parsed = parse_search_item(item)
            if parsed is None:
                continue
            repo_key = parsed.repo_full_name.lower()
            owner = repo_key.split("/")[0]
            if owner in blocked:
                stats.gate_rejected += 1
                continue
            repo_row = repo_rows.get(repo_key)
            if repo_row is None:
                repo_row = await self._resolve_unknown_repo(dal, repo_rows, parsed.repo_full_name, now)
                repo_key = repo_row.full_name.lower() if repo_row is not None else repo_key
            if repo_row is None or not repo_row.monitored:
                stats.gate_rejected += 1
                continue
            inserted = await dal.insert_issue(
                {
                    "repo_full_name": repo_row.full_name,
                    "number": parsed.number,
                    "title": parsed.title,
                    "state": parsed.state,
                    "author": parsed.author,
                    "created_at": parsed.created_at,
                    "labels": parsed.labels,
                    "assignees": parsed.assignees,
                    "raw": parsed.raw,
                    "score": 0,
                }
            )
            if not inserted:
                continue
            new_count += 1
            await self.pipeline.bump(dal, "seen")
            outcome = await self.pipeline.process_new_issue(dal, parsed, repo_row=repo_row, now=now)
            if outcome.notified_instant:
                stats.instant += 1
            if outcome.queued_digest:
                stats.digest += 1
        return new_count

    async def _resolve_unknown_repo(
        self, dal: DAL, repo_rows: dict[str, Repo], full_name: str, now: datetime
    ) -> Repo | None:
        """v2 on-demand gate: one REST fetch per unseen repo, cached in `repos`.

        Decision: owner in watch orgs AND stars >= min_stars, or exact pinned repo."""
        if "/" not in full_name:
            return None
        owner = full_name.split("/")[0]
        watch_orgs = {login.lower() for login in self.cfg.watch.orgs}
        pinned = {name.lower() for name in self.cfg.watch.pinned_repos}
        try:
            payload = await self.svc.gh.get_repo(full_name)
        except NotFound:
            await dal.bump_funnel(now.strftime("%Y-%m-%d"), "gate_404")
            return None
        except Exception:  # transient: don't cache, decide from what we know
            return None
        parsed = parse_repo_item(payload)
        if parsed is None:
            return None
        is_pinned = parsed.full_name.lower() in pinned
        is_org_member = owner.lower() in watch_orgs
        monitors = is_pinned or (is_org_member and parsed.stars >= self.cfg.sync.min_stars)
        if is_pinned and not is_org_member:
            # pinned repo whose owner we don't otherwise watch (e.g. laurent22/joplin)
            await dal.upsert_org(owner)
        await dal.upsert_repo(
            github_id=parsed.github_id,
            org_login=parsed.org_login or owner,
            full_name=parsed.full_name,
            stars=parsed.stars,
            language=parsed.language,
            archived=parsed.archived,
            pushed_at=parsed.pushed_at,
            monitored=monitors,
            is_pinned=is_pinned,
        )
        repo = await dal.find_repo(parsed.full_name)
        if repo is not None and repo.monitored:
            repo_rows[parsed.full_name.lower()] = repo
        await dal.bump_funnel(now.strftime("%Y-%m-%d"), "gate_evaluations")
        return repo
