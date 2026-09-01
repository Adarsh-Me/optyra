"""Job A — nightly repo discovery sync (report 01prd §1, §19) + org GSoC scoring.

Per org: one `search/repositories` query with the stars/activity filters upserts the
monitored whitelist (keyed by github_id so renames update full_name); repos that dropped
out are demoted, not deleted. Afterwards each org's cached GSoC relevance score is
recomputed from gsoc_years + repo metadata + our own collected issue stats.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from optyra.core.normalize import parse_repo_item
from optyra.core.scoring import map_gsoc_score
from optyra.db.dal import DAL, utcnow_aware
from optyra.db.models import Org
from optyra.services import Services

logger = logging.getLogger(__name__)

_STARTUP_DELAY_SECONDS = 30.0


def gfi_label_set(cfg) -> set[str]:
    """Raw label names that map to the canonical newcomer labels (good_first_issue, first_timers_only)."""
    targets = {"good_first_issue", "first_timers_only"}
    labels = {name for name, weight in cfg.scoring.labels.items() if name in targets and weight > 0}
    for raw, canonical in cfg.scoring.label_aliases.items():
        if canonical in targets:
            labels.add(raw)
    # include the two canonical snake_case spellings as raw labels too
    labels |= targets
    return labels


class RepoSyncJob:
    def __init__(self, services: Services) -> None:
        self.svc = services
        self.cfg = services.cfg

    async def run_forever(self) -> None:
        await asyncio.sleep(_STARTUP_DELAY_SECONDS)  # let the poller start serving first
        while True:
            started = utcnow_aware()
            try:
                result = await self.run_once()
                logger.info(
                    "repo sync: orgs=%s repos=%s demoted=%s",
                    result["orgs"],
                    result["repos"],
                    result["demoted"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("repo sync failed; continuing")
            elapsed = (utcnow_aware() - started).total_seconds()
            await asyncio.sleep(max(60.0, self.cfg.sync.interval_hours * 3600 - elapsed))

    async def run_once(self) -> dict:
        total_repos = 0
        total_demoted = 0
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                orgs = await dal.get_orgs()
        for org in orgs:
            try:
                repos, demoted = await self.sync_org(org)
                total_repos += repos
                total_demoted += demoted
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("repo sync failed for %s: %r", org.login, exc)
                continue
        for org in orgs:
            try:
                await self.compute_gsoc(org)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("gsoc scoring failed for %s: %r", org.login, exc)
        self.svc.health.last_sync_at = utcnow_aware().isoformat(timespec="seconds")
        return {"orgs": len(orgs), "repos": total_repos, "demoted": total_demoted}

    async def sync_org(self, org: Org) -> tuple[int, int]:
        """Returns (kept_repo_count, demoted_count)."""
        items = await self.svc.gh.search_repositories(
            org.login, min_stars=self.cfg.sync.min_stars, per_page=self.cfg.sync.per_page
        )
        keep_ids: set[int] = set()
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                for item in items:
                    parsed = parse_repo_item(item)
                    if parsed is None:
                        continue
                    keep_ids.add(parsed.github_id)
                    await dal.upsert_repo(
                        github_id=parsed.github_id,
                        org_login=org.login,
                        full_name=parsed.full_name,
                        stars=parsed.stars,
                        language=parsed.language,
                        archived=parsed.archived,
                        pushed_at=parsed.pushed_at,
                        monitored=True,
                    )
                demoted = await dal.demote_missing_repos(org.login, keep_ids)
        logger.info("synced %s repos for %s (%s demoted)", len(keep_ids), org.login, demoted)
        return len(keep_ids), demoted

    async def compute_gsoc(self, org: Org) -> None:
        """Cached org-level GSoC relevance score (report §11), recomputed nightly."""
        now = utcnow_aware()
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                total, gfi, triage_samples = await dal.org_issue_stats(
                    org.login,
                    since=now - timedelta(days=30),
                    gfi_labels=gfi_label_set(self.cfg),
                )
                has_mega = await dal.org_has_mega_repo(org.login, min_stars=10000, pushed_within_days=90)
        ratio = (gfi / total) if total >= 20 else None  # need a real sample to trust
        median_hours = DAL.median_or_none(triage_samples)
        score, components = map_gsoc_score(
            gsoc_years=org.gsoc_years or [],
            has_mega_repo=has_mega,
            newcomer_ratio=ratio,
            median_triage_hours=median_hours,
            cfg=self.cfg.scoring,
        )
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                await dal.update_org_gsoc(
                    org.login,
                    score,
                    {
                        **components,
                        "issues_30d": total,
                        "gfi_30d": gfi,
                        "triage_median_hours": median_hours,
                        "has_mega_repo": has_mega,
                    },
                )
