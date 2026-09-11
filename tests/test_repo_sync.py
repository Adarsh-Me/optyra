"""Repo sync tests (v2): pinned discovery for personal-account repos, demotion protection,
watchlist reporting — GSoC scoring is retired."""

from __future__ import annotations

from dataclasses import replace

from conftest import FakeGitHub, make_repo_item, make_services
from optyra.config import WatchConfig
from optyra.db.dal import DAL
from optyra.jobs.repo_sync import RepoSyncJob


def _watch(cfg, orgs=("acme",), pinned=("laurent22/joplin",)):
    return replace(
        cfg,
        watch=WatchConfig(orgs=orgs, pinned_repos=pinned, blocked_owners=frozenset({"pytorch", "rust-lang"})),
    )


async def test_sync_upserts_demotes_and_protects_pinned(db_factory, cfg):
    cfg = _watch(cfg)
    gh = FakeGitHub()
    gh.search_repo_items["acme"] = [make_repo_item("acme/widgets"), make_repo_item("acme/airflow")]
    gh.repos["laurent22/joplin"] = make_repo_item("laurent22/joplin", stars=56000)
    services, _, _, _ = make_services(cfg, db_factory, gh)
    job = RepoSyncJob(services)
    result = await job.run_once()
    assert result["orgs"] == 1 and result["repos"] == 2 and result["pinned"] == 1

    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert (await dal.find_repo("acme/widgets")).monitored is True
            joplin = await dal.find_repo("laurent22/joplin")
            assert joplin is not None and joplin.monitored is True and joplin.is_pinned is True
            # joplin's owner was seeded so reconcile won't strip it
            assert "laurent22" in {o.login for o in await dal.get_orgs()}

    # airflow disappears: demoted; widgets reappears renamed via same github_id
    renamed = make_repo_item("acme/widgets-renamed")
    renamed["id"] = (await _repo_id(db_factory, "acme/widgets")).__int__()
    gh.search_repo_items["acme"] = [renamed]
    result = await job.run_once()
    assert result["repos"] == 1 and result["demoted"] == 1
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.find_repo("acme/widgets-renamed") is not None
            assert (await dal.find_repo("acme/airflow")).monitored is False


async def test_pinned_repo_survives_star_flicker(db_factory, cfg):
    cfg = _watch(cfg, orgs=(), pinned=("acme/borderline",))
    gh = FakeGitHub()
    # 9k stars: below the gate — but pinned, so sync_pinned must still monitor it
    gh.repos["acme/borderline"] = make_repo_item("acme/borderline", stars=9000)
    services, _, _, _ = make_services(cfg, db_factory, gh)
    await RepoSyncJob(services).run_once()
    async with db_factory() as session:
        async with session.begin():
            repo = await DAL(session).find_repo("acme/borderline")
            assert repo is not None and repo.monitored is True and repo.is_pinned is True


async def test_watchlist_summary_after_sync(db_factory, cfg):
    cfg = _watch(cfg, orgs=("acme",), pinned=("laurent22/joplin", "acme/widgets"))
    gh = FakeGitHub()
    gh.search_repo_items["acme"] = [make_repo_item("acme/widgets")]
    gh.repos["laurent22/joplin"] = make_repo_item("laurent22/joplin", stars=56000)
    services, _, _, _ = make_services(cfg, db_factory, gh)
    await RepoSyncJob(services).run_once()
    async with db_factory() as session:
        async with session.begin():
            summary = await DAL(session).watchlist_summary()
    assert "acme" in summary and "laurent22" in summary
    pinned_names = [n for n, _s, pin in summary.get("laurent22", []) if pin]
    assert pinned_names == ["laurent22/joplin"]


async def _repo_id(db_factory, full_name: str) -> int:
    async with db_factory() as session:
        async with session.begin():
            repo = await DAL(session).find_repo(full_name)
            return repo.github_id
