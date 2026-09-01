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
        dialect = self.session.bind.dialect.name if self.session.bind is not None else "postgresql"
        if dialect == "sqlite":
            stmt = (
                sqlite_insert(model)
                .values(**values)
                .on_conflict_do_nothing(index_elements=list(index_elements))
            )
        else:
            stmt = (
                pg_insert(model).values(**values).on_conflict_do_nothing(index_elements=list(index_elements))
            )
        return stmt

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

    # ------------------------------------------------------------------ orgs

    async def upsert_org(self, login: str, tier: int, gsoc_years: list[int]) -> None:
        values = {"login": login, "tier": tier, "gsoc_years": gsoc_years, "created_at": utcnow()}
        stmt = self._upsert(
            Org,
            values,
            ["login"],
            {"tier": tier, "gsoc_years": gsoc_years},
        )
        await self.session.execute(stmt)

    async def get_orgs(self) -> list[Org]:
        rows = await self.session.execute(select(Org).order_by(Org.login))
        return list(rows.scalars().all())

    async def update_org_gsoc(self, login: str, score: int, components: dict) -> None:
        await self.session.execute(
            update(Org)
            .where(Org.login == login)
            .values(gsoc_score=score, gsoc_components=components, gsoc_computed_at=utcnow())
        )

    async def org_gsoc_score(self, login: str) -> int:
        row = await self.session.execute(select(Org.gsoc_score).where(Org.login == login))
        value = row.scalar_one_or_none()
        return int(value) if value is not None else 0

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
            },
        )
        await self.session.execute(stmt)

    async def demote_missing_repos(self, org_login: str, keep_ids: set[int]) -> int:
        """Repos of this org not in the latest discovery sync stop being monitored."""
        rows = await self.session.execute(
            select(Repo.github_id).where(Repo.org_login == org_login, Repo.monitored.is_(True))
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

    async def org_has_mega_repo(self, org_login: str, min_stars: int, pushed_within_days: int) -> bool:
        cutoff = utcnow_aware() - timedelta(days=pushed_within_days)
        row = await self.session.execute(
            select(Repo.github_id)
            .where(
                func.lower(Repo.org_login) == org_login.lower(),
                Repo.stars >= min_stars,
                Repo.archived.is_(False),
                Repo.pushed_at >= cutoff,
            )
            .limit(1)
        )
        return row.first() is not None

    # ------------------------------------------------------------------ issues

    async def insert_issue(self, values: dict) -> bool:
        """Insert-if-new on the composite PK. Returns True when the row is new."""
        values = {"first_seen_at": utcnow(), **values}
        stmt = self._insert_on_conflict(Issue, values, ["repo_full_name", "number"])
        result = await self.session.execute(stmt)
        return (result.rowcount or 0) > 0

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
        stmt = self._insert_on_conflict(
            Notification,
            {"issue_key": issue_key, "channel": channel, "created_at": utcnow()},
            ["issue_key", "channel"],
        )
        result = await self.session.execute(stmt)
        return (result.rowcount or 0) > 0

    async def mark_notification_sent(self, issue_key: str, channel: str) -> None:
        await self.session.execute(
            update(Notification)
            .where(Notification.issue_key == issue_key, Notification.channel == channel)
            .values(sent_at=utcnow())
        )

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

    # ------------------------------------------------------------------ gsoc stats

    async def org_issue_stats(
        self, org_login: str, since: datetime, gfi_labels: set[str]
    ) -> tuple[int, int, list[float]]:
        """(total_issues_since, good_first_issue_count, triage_hours_samples).

        Triage proxy: hours from issue creation to first observed comment, for issues where
        we captured a timeline (candidates only — a documented v1 limitation, report §11).
        """
        rows = await self.session.execute(
            select(Issue.labels, Issue.created_at, Issue.first_comment_at).where(
                func.lower(Issue.repo_full_name).startswith(org_login.lower() + "/"),
                Issue.created_at >= since,
            )
        )
        total = 0
        gfi = 0
        triage: list[float] = []
        for labels, created_at, first_comment_at in rows.all():
            total += 1
            label_set = {str(name).lower() for name in (labels or [])}
            if label_set & gfi_labels:
                gfi += 1
            if created_at and first_comment_at:
                hours = (first_comment_at - created_at).total_seconds() / 3600
                if hours >= 0:
                    triage.append(hours)
        return total, gfi, triage

    # ------------------------------------------------------------------ maintenance

    async def prune(self, *, issues_before: datetime, notifications_before: datetime) -> tuple[int, int]:
        res_issues = await self.session.execute(delete(Issue).where(Issue.first_seen_at < issues_before))
        res_notifs = await self.session.execute(
            delete(Notification).where(Notification.created_at < notifications_before)
        )
        return res_issues.rowcount or 0, res_notifs.rowcount or 0

    async def count_pending_notifications(self) -> int:
        row = await self.session.execute(
            select(func.count()).select_from(Notification).where(Notification.sent_at.is_(None))
        )
        return int(row.scalar_one())

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def median_or_none(samples: list[float]) -> float | None:
        if not samples:
            return None
        return float(statistics.median(samples))
