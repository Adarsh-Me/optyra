"""Data-access layer. All SQL lives here; jobs never touch the session directly.

Upserts are dialect-aware (PostgreSQL in production, SQLite in tests).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String as SAString
from sqlalchemy import and_, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from optyra.db.models import (
    Issue,
    MetricsDaily,
    Notification,
    Org,
    PollState,
    Repo,
    utcnow,
)


def utcnow_aware() -> datetime:
    return datetime.now(UTC)


class DAL:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ upsert helpers

    def _insert_on_conflict(self, model: type, values: dict, index_elements: Sequence[str]):
        """Insert-if-new; callers use .returning(pk) + scalar() to detect the conflict
        (works identically on asyncpg and aiosqlite)."""
        dialect = self.session.bind.dialect.name if self.session.bind is not None else "postgresql"
        stmt_cls = sqlite_insert if dialect == "sqlite" else pg_insert
        pk = list(model.__table__.primary_key.columns)[0]
        return (
            stmt_cls(model)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(index_elements))
            .returning(pk)
        )

    async def _execute_insert_if_new(self, model: type, values: dict, index_elements: Sequence[str]) -> bool:
        stmt = self._insert_on_conflict(model, values, index_elements)
        result = await self.session.execute(stmt)
        return result.scalar() is not None

    def _upsert(self, model: type, values: dict, index_elements: Sequence[str], update_set: dict):
        dialect = self.session.bind.dialect.name if self.session.bind is not None else "postgresql"
        if dialect == "sqlite":
            stmt = (
                sqlite_insert(model)
                .values(**values)
                .on_conflict_do_update(index_elements=list(index_elements), set_=update_set)
            )
        else:
            stmt = (
                pg_insert(model)
                .values(**values)
                .on_conflict_do_update(index_elements=list(index_elements), set_=update_set)
            )
        return stmt

    # ------------------------------------------------------------------ orgs / watch set

    async def upsert_org(self, login: str) -> None:
        values = {"login": login, "created_at": utcnow()}
        stmt = self._upsert(Org, values, ["login"], {"login": login})  # SET login=login: no-op update
        await self.session.execute(stmt)

    async def get_orgs(self) -> list[Org]:
        rows = await self.session.execute(select(Org).order_by(Org.login))
        return list(rows.scalars().all())

    async def reconcile_watch(self, valid_logins: set[str], valid_scopes: set[str]) -> tuple[int, int]:
        """Drop orgs + poll_state scopes that are no longer in the watch config, so a
        shrunk orgs.yaml doesn't keep polling deleted orgs forever (v2 startup).
        Case-insensitive on org logins (GitHub names preserve case)."""
        valid_logins = {login.lower() for login in valid_logins}
        valid_scopes_lower = {scope.lower() for scope in valid_scopes}
        org_rows = await self.session.execute(select(Org.login))
        stale_logins = [login for (login,) in org_rows.all() if login.lower() not in valid_logins]
        scope_rows = await self.session.execute(select(PollState.scope))
        stale_scopes = [scope for (scope,) in scope_rows.all() if scope.lower() not in valid_scopes_lower]
        if stale_logins:
            await self.session.execute(delete(Org).where(Org.login.in_(stale_logins)))
        if stale_scopes:
            await self.session.execute(delete(PollState).where(PollState.scope.in_(stale_scopes)))
        return len(stale_logins), len(stale_scopes)

    # ------------------------------------------------------------------ repos

    async def upsert_repo(
        self,
        *,
        github_id: int,
        org_login: str,
        full_name: str,
        stars: int,
        language: str | None,
        archived: bool,
        pushed_at: datetime | None,
        monitored: bool = True,
        is_pinned: bool = False,
    ) -> None:
        now = utcnow()
        values = {
            "github_id": github_id,
            "org_login": org_login,
            "full_name": full_name,
            "stars": stars,
            "language": language,
            "archived": archived,
            "monitored": monitored,
            "pushed_at": pushed_at,
            "last_synced_at": now,
            "is_pinned": is_pinned,
            "created_at": now,
        }
        stmt = self._upsert(
            Repo,
            values,
            ["github_id"],
            {
                "org_login": org_login,
                "full_name": full_name,
                "stars": stars,
                "language": language,
                "archived": archived,
                "monitored": monitored,
                "pushed_at": pushed_at,
                "last_synced_at": now,
                "is_pinned": is_pinned,
            },
        )
        await self.session.execute(stmt)

    async def demote_missing_repos(self, org_login: str, keep_ids: set[int]) -> int:
        """Repos of this org not in the latest discovery sync stop being monitored.
        Pinned repos are never demoted here (pinned discovery keeps them fresh)."""
        rows = await self.session.execute(
            select(Repo.github_id).where(
                Repo.org_login == org_login, Repo.monitored.is_(True), Repo.is_pinned.is_(False)
            )
        )
        demote = [gid for (gid,) in rows.all() if gid not in keep_ids]
        if demote:
            await self.session.execute(update(Repo).where(Repo.github_id.in_(demote)).values(monitored=False))
        return len(demote)

    async def monitored_repo_names(self) -> set[str]:
        rows = await self.session.execute(select(Repo.full_name).where(Repo.monitored.is_(True)))
        return {name.lower() for (name,) in rows.all()}

    async def monitored_repo_rows(self) -> dict[str, Repo]:
        rows = await self.session.execute(select(Repo).where(Repo.monitored.is_(True)))
        return {repo.full_name.lower(): repo for repo in rows.scalars().all()}

    async def find_repo(self, full_name: str) -> Repo | None:
        row = await self.session.execute(select(Repo).where(func.lower(Repo.full_name) == full_name.lower()))
        return row.scalar_one_or_none()

    async def mark_repo_unmonitored(self, full_name: str) -> None:
        await self.session.execute(
            update(Repo).where(func.lower(Repo.full_name) == full_name.lower()).values(monitored=False)
        )

    # ------------------------------------------------------------------ setup-weight priors (v2)

    async def record_setup_verdict(self, full_name: str, weight: str) -> None:
        """Accumulate this repo's per-repo setup prior + refresh the majority value.

        Keyed per repo (not per org): build environments differ within an org
        (tensorflow vs tfjs)."""
        repo = await self.find_repo(full_name)
        if repo is None:
            return
        prior = dict(repo.setup_prior or {"minimal": 0, "moderate": 0, "heavy": 0})
        prior[weight] = int(prior.get(weight, 0)) + 1
        majority = self.majority_weight(prior)
        await self.session.execute(
            update(Repo)
            .where(Repo.github_id == repo.github_id)
            .values(setup_prior=prior, setup_weight=majority)
        )

    async def resolve_setup_prior(self, full_name: str, min_samples: int) -> str | None:
        """The repo's majority setup weight once enough verdicts exist; None = unknown
        (fail-open default). Used on AI-down days instead of letting heavy issues through."""
        repo = await self.find_repo(full_name)
        if repo is None or not repo.setup_prior:
            return None
        total = sum(int(v) for v in repo.setup_prior.values())
        if total < min_samples:
            return None
        return repo.setup_weight or self.majority_weight(repo.setup_prior)

    @staticmethod
    def majority_weight(prior: dict) -> str | None:
        if not prior or sum(int(v) for v in prior.values()) == 0:
            return None
        return max(prior.items(), key=lambda kv: int(kv[1]))[0]

    # ------------------------------------------------------------------ issues

    async def insert_issue(self, values: dict) -> bool:
        """Insert-if-new on the composite PK. Returns True when the row is new."""
        values = {"first_seen_at": utcnow(), **values}
        return await self._execute_insert_if_new(Issue, values, ["repo_full_name", "number"])

    async def get_issue(self, repo_full_name: str, number: int) -> Issue | None:
        row = await self.session.execute(
            select(Issue).where(
                func.lower(Issue.repo_full_name) == repo_full_name.lower(),
                Issue.number == number,
            )
        )
        return row.scalar_one_or_none()

    async def update_issue(self, repo_full_name: str, number: int, values: dict) -> None:
        await self.session.execute(
            update(Issue)
            .where(
                func.lower(Issue.repo_full_name) == repo_full_name.lower(),
                Issue.number == number,
            )
            .values(**values)
        )

    async def issues_for_state_refresh(
        self, *, first_seen_after: datetime, min_score: int, limit: int
    ) -> list[Issue]:
        rows = await self.session.execute(
            select(Issue)
            .where(
                Issue.first_seen_at >= first_seen_after,
                Issue.state == "open",
                (Issue.score >= min_score) | (Issue.notified.is_(True)),
            )
            .order_by(Issue.score.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def pending_notifications(self) -> list[tuple[Notification, Issue]]:
        """Unsent notifications joined with their issue rows (for the digest flush)."""
        key_expr = Issue.repo_full_name + "#" + cast(Issue.number, SAString)
        rows = await self.session.execute(
            select(Notification, Issue)
            .join(Issue, and_(key_expr == Notification.issue_key))
            .where(Notification.sent_at.is_(None))
            .order_by(Issue.score.desc(), Issue.created_at.desc())
        )
        return [(n, i) for n, i in rows.all()]

    # ------------------------------------------------------------------ notifications

    async def insert_notification(self, issue_key: str, channel: str) -> bool:
        """Insert-if-new on (issue_key, channel). False means someone already notified."""
        return await self._execute_insert_if_new(
            Notification,
            {"issue_key": issue_key, "channel": channel, "created_at": utcnow()},
            ["issue_key", "channel"],
        )

    async def mark_notification_sent(self, issue_key: str, channel: str) -> None:
        await self.session.execute(
            update(Notification)
            .where(Notification.issue_key == issue_key, Notification.channel == channel)
            .values(sent_at=utcnow())
        )

    async def mark_notification_suppressed(self, issue_key: str, channel: str) -> None:
        """Terminal mark for budget/size-cap suppressions: the row never re-enters the
        flush, and the suppression is visible in the daily funnel report."""
        now = utcnow()
        await self.session.execute(
            update(Notification)
            .where(Notification.issue_key == issue_key, Notification.channel == channel)
            .values(sent_at=now, suppressed_at=now)
        )

    async def owner_digest_counts(
        self, day_start: datetime, *, below_score: int | None = None
    ) -> dict[str, int]:
        """v2 flush-time budget input: digest notifications actually sent today, per
        repo owner. `below_score` excludes instant-lane sends (they don't consume the
        digest budget); suppressed rows never count."""
        key_expr = Issue.repo_full_name + "#" + cast(Issue.number, SAString)
        conditions = [
            Notification.sent_at.isnot(None),
            Notification.sent_at >= day_start,
            Notification.suppressed_at.is_(None),
        ]
        if below_score is not None:
            conditions.append(Issue.score < below_score)
        rows = await self.session.execute(
            select(Issue.repo_full_name)
            .join(Notification, key_expr == Notification.issue_key)
            .where(*conditions)
        )
        counts: dict[str, int] = {}
        for (full_name,) in rows.all():
            owner = full_name.split("/")[0].lower()
            counts[owner] = counts.get(owner, 0) + 1
        return counts

    # ------------------------------------------------------------------ poll_state

    async def get_poll_state(self, scope: str) -> PollState | None:
        row = await self.session.execute(select(PollState).where(PollState.scope == scope))
        return row.scalar_one_or_none()

    async def init_poll_state(self, scope: str, watermark: datetime) -> PollState:
        existing = await self.get_poll_state(scope)
        if existing is not None:
            return existing
        stmt = self._insert_on_conflict(
            PollState,
            {"scope": scope, "watermark": watermark, "consecutive_failures": 0, "updated_at": utcnow()},
            ["scope"],
        )
        await self.session.execute(stmt)
        return await self.get_poll_state(scope)  # type: ignore[return-value]

    async def update_poll_state(
        self,
        scope: str,
        *,
        watermark: datetime | None = None,
        ok: bool | None = None,
        breaker_until: datetime | None = None,
        breaker_failures: int | None = None,
        breaker_cooldown_seconds: int = 900,
    ) -> PollState:
        state = await self.get_poll_state(scope)
        if state is None:
            state = await self.init_poll_state(scope, watermark or utcnow_aware())
        values: dict[str, Any] = {"updated_at": utcnow()}
        if watermark is not None:
            values["watermark"] = watermark
        if ok is True:
            values["last_ok"] = utcnow()
            values["consecutive_failures"] = 0
        elif ok is False:
            failures = (state.consecutive_failures or 0) + 1
            values["consecutive_failures"] = failures
            if breaker_failures is not None and failures >= breaker_failures:
                values["breaker_until"] = utcnow_aware() + timedelta(seconds=breaker_cooldown_seconds)
        if breaker_until is not None:
            values["breaker_until"] = breaker_until
        await self.session.execute(update(PollState).where(PollState.scope == scope).values(**values))
        refreshed = await self.get_poll_state(scope)
        assert refreshed is not None
        return refreshed

    # ------------------------------------------------------------------ funnel metrics (v2)

    async def bump_funnel(self, day: str, key: str, amount: int = 1) -> None:
        """Read-modify-write funnel counter for a UTC day (single worker: safe)."""
        row = await self.session.execute(select(MetricsDaily.data).where(MetricsDaily.day == day))
        data = dict(row.scalar_one_or_none() or {})
        data[key] = int(data.get(key, 0)) + amount
        stmt = self._upsert(
            MetricsDaily,
            {"day": day, "data": data, "updated_at": utcnow()},
            ["day"],
            {"data": data, "updated_at": utcnow()},
        )
        await self.session.execute(stmt)

    async def read_funnel(self, day: str) -> dict:
        row = await self.session.execute(select(MetricsDaily.data).where(MetricsDaily.day == day))
        return dict(row.scalar_one_or_none() or {})

    async def ai_calls_today(self, day: str) -> int:
        funnel = await self.read_funnel(day)
        return int(funnel.get("ai_calls", 0))

    # ------------------------------------------------------------------ maintenance

    async def prune(
        self,
        *,
        issues_before: datetime,
        notifications_before: datetime,
        metrics_before_day: str | None = None,
    ) -> tuple[int, int, int]:
        res_issues = await self.session.execute(delete(Issue).where(Issue.first_seen_at < issues_before))
        key_expr = Issue.repo_full_name + "#" + cast(Issue.number, SAString)
        orphaned = ~select(Issue.number).where(key_expr == Notification.issue_key).exists()
        res_notifs = await self.session.execute(
            delete(Notification).where((Notification.created_at < notifications_before) | orphaned)
        )
        res_metrics = 0
        if metrics_before_day:
            res_metrics = (
                await self.session.execute(delete(MetricsDaily).where(MetricsDaily.day < metrics_before_day))
            ).rowcount or 0
        return res_issues.rowcount or 0, res_notifs.rowcount or 0, res_metrics

    async def count_pending_notifications(self) -> int:
        row = await self.session.execute(
            select(func.count()).select_from(Notification).where(Notification.sent_at.is_(None))
        )
        return int(row.scalar_one())

    async def watchlist_summary(self) -> dict[str, list[tuple[str, int, bool]]]:
        """org -> [(full_name, stars, pinned)] for the daily funnel report / sync log."""
        rows = await self.session.execute(
            select(Repo.org_login, Repo.full_name, Repo.stars, Repo.is_pinned)
            .where(Repo.monitored.is_(True))
            .order_by(Repo.org_login, Repo.stars.desc())
        )
        summary: dict[str, list[tuple[str, int, bool]]] = {}
        for org, full_name, stars, pinned in rows.all():
            summary.setdefault(org, []).append((full_name, int(stars), bool(pinned)))
        return summary

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def median_or_none(samples: list[float]) -> float | None:
        if not samples:
            return None
        return float(statistics.median(samples))
