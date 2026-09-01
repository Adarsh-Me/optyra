"""Job C — hourly state refresh of recent high-score issues (report 01prd §1).

Re-checks assignment/closed transitions so the database stays truthful (the report scopes
this as dashboard support: updates only, never re-notifies).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from optyra.db.dal import DAL, utcnow_aware
from optyra.github.client import NotFound
from optyra.services import Services

logger = logging.getLogger(__name__)

_STARTUP_DELAY_SECONDS = 90.0


class StateRefreshJob:
    def __init__(self, services: Services) -> None:
        self.svc = services
        self.cfg = services.cfg

    async def run_forever(self) -> None:
        await asyncio.sleep(_STARTUP_DELAY_SECONDS)
        while True:
            started = utcnow_aware()
            try:
                checked = await self.run_once()
                if checked:
                    logger.info("state refresh: %s issue(s) updated", checked)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("state refresh failed; continuing")
            elapsed = (utcnow_aware() - started).total_seconds()
            interval = self.cfg.maintenance.state_refresh_interval_seconds
            await asyncio.sleep(max(60.0, interval - elapsed))

    async def run_once(self) -> int:
        now = utcnow_aware()
        cutoff = now - timedelta(seconds=self.cfg.maintenance.state_refresh_max_age_seconds)
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                issues = await dal.issues_for_state_refresh(
                    first_seen_after=cutoff,
                    min_score=self.cfg.notify.digest_threshold,
                    limit=self.cfg.maintenance.state_refresh_batch,
                )
        updated = 0
        for issue in issues:
            try:
                fresh = await self.svc.gh.get_issue(issue.repo_full_name, issue.number)
            except NotFound:
                # issue deleted (or repo vanished) — stop surfacing it as open
                async with self.svc.session_factory() as session:
                    async with session.begin():
                        dal = DAL(session)
                        await dal.update_issue(
                            issue.repo_full_name,
                            issue.number,
                            {"state": "closed", "last_checked_at": now},
                        )
                updated += 1
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "state refresh fetch failed for %s#%s: %r",
                    issue.repo_full_name,
                    issue.number,
                    exc,
                )
                continue
            assignees = [
                str(a.get("login"))
                for a in (fresh.get("assignees") or [])
                if isinstance(a, dict) and a.get("login")
            ]
            labels = [
                str(label.get("name")).lower()
                for label in (fresh.get("labels") or [])
                if isinstance(label, dict) and label.get("name")
            ]
            state = str(fresh.get("state") or issue.state)
            async with self.svc.session_factory() as session:
                async with session.begin():
                    dal = DAL(session)
                    await dal.update_issue(
                        issue.repo_full_name,
                        issue.number,
                        {
                            "state": state,
                            "assignees": assignees,
                            "labels": labels,
                            "last_checked_at": now,
                        },
                    )
            updated += 1
        self.svc.health.last_state_refresh_at = now.isoformat(timespec="seconds")
        return updated
