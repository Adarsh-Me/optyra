"""Report §20 Day-1 spike: prove discovery + search + scoring before any infrastructure.

Usage:
    export GH_TOKEN=github_pat_...
    python scripts/spike.py [org ...]        # defaults to the first 5 orgs in config/orgs.yaml

Prints the monitored repos per org and a ranked 24 h issue list — the exact output that
validated the whole project concept in the report. Read-only, no DB, no notifications.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from optyra.config import load_config
from optyra.core import filters as filters_mod
from optyra.core.normalize import parse_repo_item, parse_search_item
from optyra.core.scoring import score_issue
from optyra.github.client import GitHubClient


async def main() -> None:
    cfg = load_config()
    orgs = [entry.login for entry in cfg.orgs]
    args = sys.argv[1:]
    orgs = args if args else orgs[:5]
    print(f"spike: orgs={orgs} min_stars={cfg.sync.min_stars} (token ok)")

    async with GitHubClient(cfg.secrets.gh_token, bucket=None) as gh:
        monitored: dict[str, dict] = {}
        for org in orgs:
            try:
                items = await gh.search_repositories(
                    org, min_stars=cfg.sync.min_stars, per_page=100, max_pages=1
                )
            except Exception as exc:
                print(f"\n== {org}: discovery failed ({exc!r})")
                continue
            repos = [parse_repo_item(item) for item in items]
            repos = [r for r in repos if r is not None]
            print(f"\n== {org}: {len(repos)} monitored repos (top 5)")
            for repo in repos[:5]:
                print(f"   {repo.full_name:<45} ⭐{repo.stars:>7,}  {repo.language or '-'}")
            for repo in repos:
                monitored[repo.full_name.lower()] = repo

        since = datetime.now(UTC) - timedelta(hours=24)
        ranked = []
        for org in orgs:
            try:
                items = await gh.search_issues(org, since=since, max_pages=2)
            except Exception as exc:
                print(f"   (issue search failed for {org}: {exc!r})")
                continue
            for item in items:
                parsed = parse_search_item(item)
                if parsed is None or parsed.repo_full_name.lower() not in monitored:
                    continue
                result = filters_mod.hard_filter(parsed, cfg.filters, now=since)
                if not result.ok:
                    continue
                repo = monitored[parsed.repo_full_name.lower()]
                breakdown = score_issue(
                    parsed,
                    cfg.scoring,
                    repo_stars=repo.stars,
                    repo_pushed_at=repo.pushed_at,
                )
                ranked.append((breakdown.total, parsed, repo))
            await asyncio.sleep(1.0)  # search pacing without a bucket

        ranked.sort(key=lambda triple: triple[0], reverse=True)
        print(f"\n== last 24h: {len(ranked)} candidate issues after hard filters (top 20)")
        for score, parsed, _repo in ranked[:20]:
            labels = ",".join(parsed.labels[:3]) or "-"
            print(
                f"  {score:>3}  {parsed.repo_full_name}#{parsed.number:<5} {parsed.title[:70]:<70} [{labels}]"
            )
        if not ranked:
            print("  (no candidates in the last 24h — widen the window or lower min_stars)")


if __name__ == "__main__":
    asyncio.run(main())
