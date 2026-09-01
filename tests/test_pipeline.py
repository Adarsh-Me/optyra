"""End-to-end pipeline tests: sync -> poll -> filter -> score -> deep-check -> enrich ->
notify (instant + digest) -> dedupe -> state refresh. Fakes for GitHub/AI/Telegram."""

from __future__ import annotations

from datetime import timedelta

from conftest import (
    FakeAI,
    FakeGitHub,
    FakeTelegram,
    ai_response,
    make_issue_item,
    make_repo_item,
    make_services,
    utcnow,
)
from optyra.db.dal import DAL
from optyra.db.models import Org
from optyra.jobs.issue_poll import IssuePoller
from optyra.jobs.maintenance import DigestFlushJob
from optyra.jobs.state_refresh import StateRefreshJob

AI_SUMMARY = {
    "summary": "Kafka connector hits NPE with a custom partitioner; small null-check fix.",
    "worth_attempting": True,
    "reason_codes": ["good-fit"],
    "difficulty": "medium",
}


async def seed_org(db_factory, login: str = "apache", tier: int = 1):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.upsert_org(login, tier, [2024, 2025])
    return Org(login=login, tier=tier, gsoc_years=[2024, 2025])


def build_world(cfg, db_factory, *, with_ai=True, with_tg=True):
    gh = FakeGitHub()
    gh.search_repo_items["apache"] = [make_repo_item("apache/kafka", stars=28000)]
    # A: full-house qualifying issue -> score 95 -> instant
    gh.search_issues_items["apache"] = [
        make_issue_item("apache/kafka", 1, created_min_ago=5),
        # B: already assigned -> hard filter
        make_issue_item("apache/kafka", 2, assignees=("bob",), created_min_ago=10, labels=("bug",)),
        # C: repo not in the monitored whitelist -> dropped entirely
        make_issue_item("apache/tinyrepo", 3, created_min_ago=8, labels=("good first issue",)),
        # D: 5h old + help wanted -> score 77 -> digest
        make_issue_item("apache/kafka", 4, created_min_ago=300, labels=("help wanted",)),
    ]
    created_1h = utcnow() - timedelta(hours=1)
    gh.timelines[("apache/kafka", 1)] = [
        {
            "event": "commented",
            "created_at": created_1h.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "actor": {"login": "maintainer"},
        },
    ]
    gh.issues[("apache/kafka", 1)] = make_issue_item("apache/kafka", 1, created_min_ago=5)
    gh.issues[("apache/kafka", 4)] = make_issue_item(
        "apache/kafka", 4, created_min_ago=300, labels=("help wanted",)
    )
    tg = FakeTelegram() if with_tg else None
    ai = FakeAI([ai_response(AI_SUMMARY), ai_response(AI_SUMMARY)]) if with_ai else None
    services, gh_client, tg_notifier, enricher = make_services(cfg, db_factory, gh, tg=tg, ai=ai)
    return gh, tg, ai, services


async def run_poll_once(services, org: Org):
    poller = IssuePoller(services)
    poller._next_due[org.login] = utcnow() - timedelta(seconds=1)  # force due
    return await poller.sweep()


async def test_full_pipeline_instant_and_digest(cfg, db_factory):
    org = await seed_org(db_factory)
    gh, tg, ai, services = build_world(cfg, db_factory)

    # Job A: repo discovery puts apache/kafka on the whitelist
    from optyra.jobs.repo_sync import RepoSyncJob

    sync_result = await RepoSyncJob(services).run_once()
    assert sync_result["repos"] == 1

    # Job B: one sweep over the org
    stats = await run_poll_once(services, org)
    assert stats.polled == 1
    assert stats.new_issues == 3  # A, B, D (C not whitelisted)
    assert stats.instant == 1  # A
    assert stats.digest == 1  # D
    assert stats.errors == 0

    # DB assertions
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issue_a = await dal.get_issue("apache/kafka", 1)
            issue_b = await dal.get_issue("apache/kafka", 2)
            issue_c = await dal.get_issue("apache/tinyrepo", 3)
            issue_d = await dal.get_issue("apache/kafka", 4)
            assert issue_c is None
            assert issue_b.filtered_reason == "assigned" and issue_b.score == 0
            assert issue_a.score == 95 and issue_a.notified is True
            assert issue_a.ai_summary.startswith("Kafka connector")
            assert issue_a.ai_worth_attempting is True
            # org score cached by the sync's nightly compute_gsoc:
            # [2024, 2025] -> 2 participations -> 20, kafka mega-repo -> 20
            assert issue_a.gsoc_score == 40
            assert issue_a.first_comment_at is not None  # captured for GSoC triage
            assert issue_d.score == 77 and issue_d.notified is True
            pending = await dal.pending_notifications()
            assert [n.issue_key for n, _ in pending] == ["apache/kafka#4"]

    # Telegram: exactly one instant message with the button
    assert len(tg.sent) == 1
    payload = tg.sent[0]
    assert "🔥 <b>95</b>" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("issues/1")
    assert ai.calls == 2  # A (95) and D (77) both pass the digest threshold -> both enriched

    # Second sweep over the same results: everything dedupes, nothing re-sends
    ai.calls = 0
    stats2 = await run_poll_once(services, org)
    assert stats2.new_issues == 0 and stats2.instant == 0
    assert len(tg.sent) == 1
    assert ai.calls == 0


async def test_digest_flush_sends_ranked_and_marks_sent(cfg, db_factory):
    org = await seed_org(db_factory)
    gh, tg, ai, services = build_world(cfg, db_factory)

    from optyra.jobs.repo_sync import RepoSyncJob

    await RepoSyncJob(services).run_once()
    await run_poll_once(services, org)

    flusher = DigestFlushJob(services)
    delivered = await flusher.flush_once()
    assert delivered == 1
    assert len(tg.sent) == 2  # instant (A) + digest (D)
    digest_text = tg.sent[1]["text"]
    assert "Optyra digest" in digest_text and "apache/kafka#4" in digest_text

    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.pending_notifications() == []
            issue_d = await dal.get_issue("apache/kafka", 4)
            assert issue_d.notified is True

    # A second flush with nothing pending is a no-op
    assert await flusher.flush_once() == 0


async def test_state_refresh_picks_up_assignment(cfg, db_factory):
    org = await seed_org(db_factory)
    gh, tg, ai, services = build_world(cfg, db_factory)

    from optyra.jobs.repo_sync import RepoSyncJob

    await RepoSyncJob(services).run_once()
    await run_poll_once(services, org)

    # Issue A gets claimed after notification
    claimed = make_issue_item("apache/kafka", 1, assignees=("bob",), created_min_ago=5)
    gh.issues[("apache/kafka", 1)] = claimed

    updated = await StateRefreshJob(services).run_once()
    assert updated >= 1
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issue_a = await dal.get_issue("apache/kafka", 1)
            assert issue_a.assignees == ["bob"]
    # no re-notification happened
    assert len(tg.sent) == 1


async def test_instant_send_failure_self_heals_via_digest(cfg, db_factory):
    org = await seed_org(db_factory)
    gh, tg, ai, services = build_world(cfg, db_factory)
    tg.status_sequence.clear()
    tg.status_sequence.extend([429, 429])  # A's instant send: 429 twice -> failed

    from optyra.jobs.repo_sync import RepoSyncJob

    await RepoSyncJob(services).run_once()
    stats = await run_poll_once(services, org)
    assert stats.instant == 0 and stats.digest == 2  # A failed to send, D queued
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            pending = await dal.pending_notifications()
            assert [n.issue_key for n, _ in pending] == ["apache/kafka#1", "apache/kafka#4"]

    delivered = await DigestFlushJob(services).flush_once()
    assert delivered == 2  # A self-healed into the digest alongside D
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.pending_notifications() == []


async def test_deep_check_blocks_claimed_issue(cfg, db_factory):
    """Issue passes search filters but is assigned by the time of the deep-check."""
    org = await seed_org(db_factory)
    gh = FakeGitHub()
    gh.search_repo_items["apache"] = [make_repo_item("apache/kafka", stars=28000)]
    gh.search_issues_items["apache"] = [make_issue_item("apache/kafka", 9, created_min_ago=3)]
    # deep-check sees an assignee that search-index lag hid
    gh.issues[("apache/kafka", 9)] = make_issue_item("apache/kafka", 9, assignees=("bob",), created_min_ago=3)
    ai = FakeAI([ai_response(AI_SUMMARY)])
    tg = FakeTelegram()
    services, _, _, _ = make_services(cfg, db_factory, gh, tg=tg, ai=ai)

    from optyra.jobs.repo_sync import RepoSyncJob

    await RepoSyncJob(services).run_once()
    stats = await run_poll_once(services, org)
    assert stats.instant == 0 and stats.digest == 0 and ai.calls == 0
    assert len(tg.sent) == 0
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issue = await dal.get_issue("apache/kafka", 9)
            assert issue.filtered_reason == "assigned-at-deepcheck"
            assert issue.score == 0
