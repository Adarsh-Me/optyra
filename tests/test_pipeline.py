"""End-to-end pipeline tests (v2): sync -> poll -> gate -> filter -> score -> deep-check ->
enrich -> setup-policy -> lane-gate -> notify (instant + digest, budgeted flush) -> dedupe
-> state refresh -> funnel counters. Fakes for GitHub/AI/Telegram."""

from __future__ import annotations

from dataclasses import replace
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
from optyra.config import WatchConfig
from optyra.db.dal import DAL
from optyra.db.models import Org
from optyra.jobs.issue_poll import IssuePoller
from optyra.jobs.maintenance import DigestFlushJob
from optyra.jobs.repo_sync import RepoSyncJob
from optyra.jobs.state_refresh import StateRefreshJob

ORG = "acme"


def _ai(summary_extra: dict | None = None, worth: bool = True, setup: str = "moderate") -> dict:
    payload = {
        "summary": "Connector hits NPE with a custom partitioner; small null-check fix.",
        "worth_attempting": worth,
        "reason_codes": ["good-fit"] if worth else ["env-heavy"],
        "difficulty": "medium",
        "setup_weight": setup,
    }
    payload.update(summary_extra or {})
    return ai_response(payload)


def v2_cfg(cfg):
    """Real config, but the watch set pointed at the test world."""
    return replace(
        cfg,
        watch=WatchConfig(orgs=(ORG,), pinned_repos=(), blocked_owners=frozenset({"pytorch", "rust-lang"})),
    )


async def seed_org(db_factory, login: str = ORG):
    async with db_factory() as session:
        async with session.begin():
            await DAL(session).upsert_org(login)
    return Org(login=login)


def build_world(cfg, db_factory, *, with_ai=True, with_tg=True):
    gh = FakeGitHub()
    gh.search_repo_items[ORG] = [make_repo_item("acme/widgets", stars=28000)]
    gh.search_issues_items[ORG] = [
        # A: full house -> 25+20+15+20+3+4+5 = 92 -> instant
        make_issue_item("acme/widgets", 1, created_min_ago=5),
        # B: assigned -> hard filter
        make_issue_item("acme/widgets", 2, assignees=("bob",), created_min_ago=10, labels=("bug",)),
        # C: unknown small repo -> on-demand gate rejects (GET /repos 404 fallback)
        make_issue_item("acme/tinyrepo", 3, created_min_ago=8, labels=("good first issue",)),
        # D: 5h old, help wanted -> 12+20+15+12+3+4+5 = 71 -> digest (signal label ok)
        make_issue_item("acme/widgets", 4, created_min_ago=300, labels=("help wanted",)),
        # E: fresh but only `bug` label, AI says not worth -> 75 -> lane gate drop
        make_issue_item("acme/widgets", 5, created_min_ago=7, labels=("bug",)),
        # F: gfi + AI setup_weight=heavy + hard policy -> setup drop
        make_issue_item("acme/widgets", 6, created_min_ago=9),
    ]
    created_1h = utcnow() - timedelta(hours=1)
    gh.timelines[("acme/widgets", 1)] = [
        {
            "event": "commented",
            "created_at": created_1h.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "actor": {"login": "maintainer"},
        },
    ]
    for n, minutes, labels in (
        (1, 5, ("good first issue",)),
        (4, 300, ("help wanted",)),
        (5, 7, ("bug",)),
        (6, 9, ("good first issue",)),
    ):
        gh.issues[("acme/widgets", n)] = make_issue_item(
            "acme/widgets", n, created_min_ago=minutes, labels=labels
        )
    tg = FakeTelegram() if with_tg else None
    ai = FakeAI([_ai(), _ai(), _ai(worth=False), _ai(setup="heavy")]) if with_ai else None
    services, _, _, _ = make_services(cfg, db_factory, gh, tg=tg, ai=ai)
    return gh, tg, ai, services


async def run_poll_once(services, scope: str):
    poller = IssuePoller(services)
    poller._next_due[scope] = utcnow() - timedelta(seconds=1)  # force due
    return await poller.sweep()


async def test_full_pipeline_instant_digest_gates_and_funnel(cfg, db_factory):
    cfg = v2_cfg(cfg)
    await seed_org(db_factory)
    gh, tg, ai, services = build_world(cfg, db_factory)

    sync_result = await RepoSyncJob(services).run_once()
    assert sync_result["repos"] == 1

    stats = await run_poll_once(services, ORG)
    assert stats.polled == 1
    assert stats.new_issues == 5  # A,B,D,E,F (C never passes the repo gate)
    assert stats.gate_rejected == 1
    assert stats.instant == 1  # A
    assert stats.digest == 1  # D queued

    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issue_a = await dal.get_issue("acme/widgets", 1)
            issue_b = await dal.get_issue("acme/widgets", 2)
            issue_c = await dal.get_issue("acme/tinyrepo", 3)
            issue_d = await dal.get_issue("acme/widgets", 4)
            issue_e = await dal.get_issue("acme/widgets", 5)
            issue_f = await dal.get_issue("acme/widgets", 6)
            assert issue_c is None  # gate-rejected before insert
            assert issue_b.filtered_reason == "assigned" and issue_b.score == 0
            assert issue_a.score == 92 and issue_a.notified is True
            assert issue_a.ai_summary.startswith("Connector")
            assert issue_a.setup_weight == "moderate"
            assert issue_a.first_comment_at is not None
            assert issue_d.score == 71 and issue_d.notified is True
            assert issue_e.filtered_reason == "lane-gate-no-signal" and issue_e.notified is False
            assert issue_f.filtered_reason == "setup-heavy"
            assert issue_f.score == 49  # capped, never instant-eligible
            pending = await dal.pending_notifications()
            assert [n.issue_key for n, _ in pending] == ["acme/widgets#4"]

    # one instant message for A (92), button to the issue
    assert len(tg.sent) == 1
    assert "🔥 <b>92</b>" in tg.sent[0]["text"]
    assert tg.sent[0]["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("issues/1")
    assert ai.calls == 4  # A, D, E, F (B/C never enriched)

    # funnel counters persisted for today
    day = utcnow().strftime("%Y-%m-%d")
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            funnel = await dal.read_funnel(day)
            assert funnel["seen"] == 5
            assert funnel["hard_filter"] == 1
            assert funnel["lane_gate"] == 1
            assert funnel["setup_dropped"] == 1
            assert funnel["notified_instant"] == 1
            assert funnel["queued_digest"] == 1
            assert funnel["ai_calls"] == 4
            assert funnel[f"owner:{ORG}:seen"] == 5

    # Second sweep over the same results: dedupe absorbs, nothing re-sends
    ai.calls = 0
    stats2 = await run_poll_once(services, ORG)
    assert stats2.new_issues == 0 and stats2.instant == 0
    assert len(tg.sent) == 1
    assert ai.calls == 0


async def test_digest_flush_marks_sent(cfg, db_factory):
    cfg = v2_cfg(cfg)
    await seed_org(db_factory)
    gh, tg, ai, services = build_world(cfg, db_factory)
    await RepoSyncJob(services).run_once()
    await run_poll_once(services, ORG)

    delivered = await DigestFlushJob(services).flush_once()
    assert delivered == 1
    assert len(tg.sent) == 2  # instant (A) + digest (D)
    digest_text = tg.sent[1]["text"]
    assert "Optyra digest" in digest_text and "acme/widgets#4" in digest_text

    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.pending_notifications() == []
            issue_d = await dal.get_issue("acme/widgets", 4)
            assert issue_d.notified is True

    assert await DigestFlushJob(services).flush_once() == 0


async def test_per_owner_budget_suppresses_overflow(cfg, db_factory):
    """7 digest issues from one owner -> first 5 sent (ranked), 2 suppressed (terminal)."""
    cfg = replace(v2_cfg(cfg), notify=replace(cfg.notify, per_owner_daily_cap=5))
    await seed_org(db_factory)
    gh = FakeGitHub()
    gh.search_repo_items[ORG] = [make_repo_item("acme/widgets", stars=28000)]
    gh.search_issues_items[ORG] = [
        make_issue_item("acme/widgets", n, created_min_ago=60 + n, labels=("help wanted",))
        for n in range(1, 8)
    ]
    for n in range(1, 8):
        gh.issues[("acme/widgets", n)] = make_issue_item(
            "acme/widgets", n, created_min_ago=60 + n, labels=("help wanted",)
        )
    ai = FakeAI([_ai() for _ in range(7)])
    tg = FakeTelegram()
    services, _, _, _ = make_services(cfg, db_factory, gh, tg=tg, ai=ai)
    await RepoSyncJob(services).run_once()
    stats = await run_poll_once(services, ORG)
    assert stats.digest == 7

    delivered = await DigestFlushJob(services).flush_once()
    assert delivered == 5  # budget cap
    assert "2 more suppressed" in tg.sent[-1]["text"]
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.pending_notifications() == []  # suppressed rows are terminal
            assert (await dal.read_funnel(utcnow().strftime("%Y-%m-%d")))["budget_suppressed"] == 2


async def test_state_refresh_picks_up_assignment(cfg, db_factory):
    cfg = v2_cfg(cfg)
    await seed_org(db_factory)
    gh, tg, ai, services = build_world(cfg, db_factory)
    await RepoSyncJob(services).run_once()
    await run_poll_once(services, ORG)

    claimed = make_issue_item("acme/widgets", 1, assignees=("bob",), created_min_ago=5)
    gh.issues[("acme/widgets", 1)] = claimed

    updated = await StateRefreshJob(services).run_once()
    assert updated >= 1
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issue_a = await dal.get_issue("acme/widgets", 1)
            assert issue_a.assignees == ["bob"]
    assert len(tg.sent) == 1  # no re-notification


async def test_instant_send_failure_self_heals_via_digest(cfg, db_factory):
    cfg = v2_cfg(cfg)
    await seed_org(db_factory)
    gh, tg, ai, services = build_world(cfg, db_factory)
    tg.status_sequence.clear()
    tg.status_sequence.extend([429, 429])  # A's instant send: 429 twice -> failed
    await RepoSyncJob(services).run_once()
    stats = await run_poll_once(services, ORG)
    assert stats.instant == 0 and stats.digest == 2  # A failed->requeued, D queued
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            pending = await dal.pending_notifications()
            assert [n.issue_key for n, _ in pending] == ["acme/widgets#1", "acme/widgets#4"]

    # instant-lane retry (A) is exempt from the owner budget; D fits within it
    delivered = await DigestFlushJob(services).flush_once()
    assert delivered == 2
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.pending_notifications() == []


async def test_deep_check_blocks_claimed_issue(cfg, db_factory):
    cfg = v2_cfg(cfg)
    await seed_org(db_factory)
    gh = FakeGitHub()
    gh.search_repo_items[ORG] = [make_repo_item("acme/widgets", stars=28000)]
    gh.search_issues_items[ORG] = [make_issue_item("acme/widgets", 9, created_min_ago=3)]
    gh.issues[("acme/widgets", 9)] = make_issue_item("acme/widgets", 9, assignees=("bob",), created_min_ago=3)
    ai = FakeAI([_ai()])
    tg = FakeTelegram()
    services, _, _, _ = make_services(cfg, db_factory, gh, tg=tg, ai=ai)

    await RepoSyncJob(services).run_once()
    stats = await run_poll_once(services, ORG)
    assert stats.instant == 0 and stats.digest == 0 and ai.calls == 0
    assert len(tg.sent) == 0
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issue = await dal.get_issue("acme/widgets", 9)
            assert issue.filtered_reason == "assigned-at-deepcheck"
            assert issue.score == 0


async def test_setup_soft_policy_demotes_instead_of_dropping(cfg, db_factory):
    """Org override 'soft': heavy verdict caps below instant -> digest-only, not dropped."""
    cfg = replace(
        v2_cfg(cfg),
        ai=replace(cfg.ai, org_setup_filter={ORG: "soft"}),
    )
    await seed_org(db_factory)
    gh = FakeGitHub()
    gh.search_repo_items[ORG] = [make_repo_item("acme/widgets", stars=50000)]
    # 25+20+15+20+3+5+5 = 93 (instant) with heavy verdict -> soft-capped to 84 -> digest
    gh.search_issues_items[ORG] = [make_issue_item("acme/widgets", 11, created_min_ago=5)]
    gh.issues[("acme/widgets", 11)] = make_issue_item("acme/widgets", 11, created_min_ago=5)
    ai = FakeAI([_ai(setup="heavy")])
    tg = FakeTelegram()
    services, _, _, _ = make_services(cfg, db_factory, gh, tg=tg, ai=ai)
    await RepoSyncJob(services).run_once()
    stats = await run_poll_once(services, ORG)
    assert stats.instant == 0 and stats.digest == 1
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issue = await dal.get_issue("acme/widgets", 11)
            assert issue.score == 84 and issue.filtered_reason is None
            assert issue.setup_weight == "heavy"
    delivered = await DigestFlushJob(services).flush_once()
    assert delivered == 1


async def test_setup_prior_gates_ai_down_days(cfg, db_factory):
    """When Gemini fails, a repo whose prior majority is 'heavy' still gets dropped."""
    cfg = v2_cfg(cfg)
    await seed_org(db_factory)
    gh = FakeGitHub()
    gh.search_repo_items[ORG] = [make_repo_item("acme/widgets", stars=28000)]
    gh.search_issues_items[ORG] = [make_issue_item("acme/widgets", 12, created_min_ago=5)]
    gh.issues[("acme/widgets", 12)] = make_issue_item("acme/widgets", 12, created_min_ago=5)
    ai = FakeAI([500, 500, 500, 500])  # every enrich attempt fails
    tg = FakeTelegram()
    services, _, _, _ = make_services(cfg, db_factory, gh, tg=tg, ai=ai)
    await RepoSyncJob(services).run_once()
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            for _ in range(5):  # prior >= prior_min_samples(5)
                await dal.record_setup_verdict("acme/widgets", "heavy")

    stats = await run_poll_once(services, ORG)
    assert stats.digest == 0 and stats.instant == 0
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issue = await dal.get_issue("acme/widgets", 12)
            assert issue.filtered_reason == "setup-heavy"
            assert (await dal.read_funnel(utcnow().strftime("%Y-%m-%d")))["ai_failures"] == 1

    # fresh repo without enough history: fail-OPEN (notification flows)
    gh2 = FakeGitHub()
    gh2.search_repo_items[ORG] = [make_repo_item("acme/brandnew", stars=28000)]
    gh2.search_issues_items[ORG] = [make_issue_item("acme/brandnew", 1, created_min_ago=5)]
    gh2.issues[("acme/brandnew", 1)] = make_issue_item("acme/brandnew", 1, created_min_ago=5)
    services2, _, _, _ = make_services(cfg, db_factory, gh2, tg=FakeTelegram(), ai=ai)
    await RepoSyncJob(services2).run_once()
    stats2 = await run_poll_once(services2, ORG)
    assert stats2.digest == 1 or stats2.instant == 1  # allowed through, no prior to judge
