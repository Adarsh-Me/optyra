"""Job A — repo discovery/metadata sync (v2).

Per org: `search/repositories` with the stars/activity filter upserts repo metadata
(keyed by github_id, so renames update full_name); repos that dropped out are demoted
(not deleted) — pinned repos are never demoted. Additionally, pinned repos are fetched
directly (GET /repos) because some are owned by personal accounts that `org:` discovery
can never see (laurent22/joplin).

v2 note: the GSoC relevance score is retired from this job — with a curated mission list
the 40-point org-history factor stopped discriminating; the correctness gate now lives
client-side in the poller's on-demand repo check, so discovery is metadata only.
"""

from __future__ import annotations

import asyncio
import logging

from optyra.core.normalize import parse_repo_item
from optyra.db.dal import DAL, utcnow_aware
from optyra.github.client import NotFound
from optyra.services import Services

logger = logging.getLogger(__name__)

_STARTUP_DELAY_SECONDS = 30.0


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
                    "repo sync: orgs=%s repos=%s demoted=%s pinned_ok=%s",
                    result["orgs"],
                    result["repos"],
                    result["demoted"],
                    result["pinned"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("repo sync failed; continuing")
            elapsed = (utcnow_aware() - started).total_seconds()
            await asyncio.sleep(max(60.0, self.cfg.sync.interval_hours * 3600 - elapsed))

    async def run_once(self) -> dict:
        """Config-driven: sync exactly the orgs in orgs.yaml (not stale DB rows)."""
        total_repos = 0
        total_demoted = 0
        for login in self.cfg.watch.orgs:
            try:
                repos, demoted = await self.sync_org(login)
                total_repos += repos
                total_demoted += demoted
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("repo sync failed for %s: %r", login, exc)
                continue
        pinned_ok = await self.sync_pinned()
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                watchlist = await dal.watchlist_summary()
        for org_login, repos in watchlist.items():
            logger.info(
                "watchlist %s: %s",
                org_login,
                ", ".join(f"{name}({stars}{'/pinned' if pin else ''})" for name, stars, pin in repos[:12]),
            )
        self.svc.health.last_sync_at = utcnow_aware().isoformat(timespec="seconds")
        return {
            "orgs": len(self.cfg.watch.orgs),
            "repos": total_repos,
            "demoted": total_demoted,
            "pinned": pinned_ok,
        }

    async def sync_org(self, login: str) -> tuple[int, int]:
        items = await self.svc.gh.search_repositories(
            login, min_stars=self.cfg.sync.min_stars, per_page=self.cfg.sync.per_page
        )
        keep_ids: set[int] = set()
        pinned = {name.lower() for name in self.cfg.watch.pinned_repos}
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                for item in items:
                    parsed = parse_repo_item(item)
                    if parsed is None:
                        continue
                    keep_ids.add(parsed.github_id)
                    is_pinned = parsed.full_name.lower() in pinned
                    await dal.upsert_repo(
                        github_id=parsed.github_id,
                        org_login=login,
                        full_name=parsed.full_name,
                        stars=parsed.stars,
                        language=parsed.language,
                        archived=parsed.archived,
                        pushed_at=parsed.pushed_at,
                        monitored=True,
                        is_pinned=is_pinned,
                    )
                demoted = await dal.demote_missing_repos(login, keep_ids)
        logger.info("synced %s repos for %s (%s demoted)", len(keep_ids), login, demoted)
        return len(keep_ids), demoted

    async def sync_pinned(self) -> int:
        """Direct discovery for every pinned repo — including personal-account owners."""
        ok = 0
        for full_name in self.cfg.watch.pinned_repos:
            try:
                payload = await self.svc.gh.get_repo(full_name)
            except NotFound:
                logger.warning("pinned repo not found (renamed/removed?): %s", full_name)
                continue
            except Exception as exc:
                logger.warning("pinned repo fetch failed for %s: %r", full_name, exc)
                continue
            parsed = parse_repo_item(payload)
            if parsed is None:
                continue
            owner = parsed.org_login or full_name.split("/")[0]
            async with self.svc.session_factory() as session:
                async with session.begin():
                    dal = DAL(session)
                    if owner.lower() not in {o.lower() for o in self.cfg.watch.orgs}:
                        await dal.upsert_org(owner)
                    await dal.upsert_repo(
                        github_id=parsed.github_id,
                        org_login=owner,
                        full_name=parsed.full_name,
                        stars=parsed.stars,
                        language=parsed.language,
                        archived=parsed.archived,
                        pushed_at=parsed.pushed_at,
                        monitored=True,
                        is_pinned=True,
                    )
            ok += 1
        return ok
