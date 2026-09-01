"""Repo sync + nightly GSoC scoring tests."""

from __future__ import annotations

from datetime import timedelta

from conftest import FakeGitHub, issue_values, make_repo_item, make_services, utcnow
from optyra.db.dal import DAL
from optyra.db.models import Org
from optyra.jobs.repo_sync import RepoSyncJob, gfi_label_set


async def test_sync_upserts_and_renames(db_factory, cfg):
    gh = FakeGitHub()
    gh.search_repo_items["apache"] = [
        make_repo_item("apache/kafka", stars=28000),
        make_repo_item("apache/airflow", stars=35000),
    ]
    services, _, _, _ = make_services(cfg, db_factory, gh)
    org = Org(login="apache", tier=1, gsoc_years=[2024, 2025])
    async with db_factory() as session:
        async with session.begin():
            await DAL(session).upsert_org(org.login, org.tier, org.gsoc_years)
    job = RepoSyncJob(services)
    result = await job.run_once()
    assert result["orgs"] == 1 and result["repos"] == 2 and result["demoted"] == 0

    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            repo = await dal.find_repo("apache/kafka")
            assert repo.monitored is True and repo.stars == 28000

    # kafka renamed + airflow disappears: same github_id updates full_name, old name demoted
    gh.search_repo_items["apache"] = [make_repo_item("apache/kafka-renamed", stars=28000)]
    gh.search_repo_items["apache"][0]["id"] = await _repo_id(db_factory, "apache/kafka")
    result = await job.run_once()
    assert result["repos"] == 1 and result["demoted"] == 1

    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.find_repo("apache/kafka-renamed") is not None
            old = await dal.find_repo("apache/airflow")
            assert old.monitored is False


async def _repo_id(db_factory, full_name: str) -> int:
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            repo = await dal.find_repo(full_name)
            return repo.github_id


async def test_gsoc_score_computation(db_factory, cfg):
    gh = FakeGitHub()
    gh.search_repo_items["apache"] = [make_repo_item("apache/kafka", stars=28000)]
    services, _, _, _ = make_services(cfg, db_factory, gh)
    org = Org(login="apache", tier=1, gsoc_years=[2020, 2021, 2022, 2023, 2024, 2025])
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.upsert_org(org.login, org.tier, org.gsoc_years)
    job = RepoSyncJob(services)
    await job.sync_org(org)

    # seed own-data signals: newcomer labels + fast triage samples
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            for number in range(1, 21):
                labels = ["good first issue"] if number % 4 == 0 else ["bug"]
                created = utcnow() - timedelta(days=number % 28 + 1)
                await dal.insert_issue(issue_values(number, labels=labels, created_at=created))
                await dal.update_issue(
                    "apache/kafka",
                    number,
                    {
                        "first_comment_at": created + timedelta(hours=6),
                    },
                )

    await job.compute_gsoc(org)
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            org_row = (await dal.get_orgs())[0]
            assert org_row.gsoc_score is not None
            components = org_row.gsoc_components
            # 6 participations -> 40, 28k-star repo pushed yesterday -> 20
            assert components["gsoc_years"] == 40
            assert components["mega_repo"] == 20
            # 5 of 20 issues gfi = 25% -> full newcomer points
            assert components["newcomer_ratio"] == 20
            # median triage ~ (6h + label offset) <= 48h -> full triage points
            assert components["triage"] == 20
            assert org_row.gsoc_score == 100


def test_gfi_label_set_from_config(cfg):
    labels = gfi_label_set(cfg)
    assert "good_first_issue" in labels
    assert "good first issue" in labels  # alias
    assert "first-timers-only" in labels  # alias
