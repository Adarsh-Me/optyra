"""DAL tests on SQLite (v2: dedupe PKs, poll_state, notifications+suppression, funnel,
priors, reconcile, budget counts, prune)."""

from __future__ import annotations

from datetime import timedelta

from conftest import issue_values, utcnow
from optyra.db.dal import DAL


async def test_issue_dedupe_on_composite_pk(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.insert_issue(issue_values(1)) is True
            assert await dal.insert_issue(issue_values(1)) is False  # dedupe
            issue = await dal.get_issue("acme/widgets", 1)
            assert issue is not None
            assert issue.issue_key == "acme/widgets#1"
            assert issue.owner == "acme"


async def test_notification_exactly_once(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.insert_notification("acme/widgets#1", "telegram") is True
            assert await dal.insert_notification("acme/widgets#1", "telegram") is False
            await dal.mark_notification_sent("acme/widgets#1", "telegram")
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.pending_notifications() == []
            assert await dal.count_pending_notifications() == 0


async def test_suppressed_notifications_are_terminal(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.insert_issue(issue_values(1, score=71))
            await dal.insert_notification("acme/widgets#1", "telegram")
            await dal.mark_notification_suppressed("acme/widgets#1", "telegram")
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.pending_notifications() == []  # never re-enters the flush


async def test_owner_digest_counts_budget_input(db_factory):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            # digest-lane (score 71) sent today -> counts
            await dal.insert_issue(issue_values(1, score=71))
            await dal.insert_notification("acme/widgets#1", "telegram")
            await dal.mark_notification_sent("acme/widgets#1", "telegram")
            # instant-lane (score 92) sent today -> excluded via below_score
            await dal.insert_issue(issue_values(2, score=92))
            await dal.insert_notification("acme/widgets#2", "telegram")
            await dal.mark_notification_sent("acme/widgets#2", "telegram")
            # suppressed -> never counts
            await dal.insert_issue(issue_values(3, score=71))
            await dal.insert_notification("acme/widgets#3", "telegram")
            await dal.mark_notification_suppressed("acme/widgets#3", "telegram")
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            counts = await dal.owner_digest_counts(day_start, below_score=85)
            assert counts == {"acme": 1}


async def test_pending_notifications_join_issue(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.insert_issue(issue_values(1, score=90, notified=True))
            await dal.insert_notification("acme/widgets#1", "telegram")
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            pending = await dal.pending_notifications()
            assert len(pending) == 1
            notification, issue = pending[0]
            assert notification.issue_key == "acme/widgets#1"
            assert issue.score == 90
            assert issue.html_url.endswith("/issues/1")


async def test_poll_state_lifecycle(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            state = await dal.init_poll_state("acme", utcnow())
            assert state.consecutive_failures == 0
            for _ in range(5):
                state = await dal.update_poll_state(
                    "acme", ok=False, breaker_failures=5, breaker_cooldown_seconds=900
                )
            assert state.consecutive_failures == 5
            breaker_until = state.breaker_until
            assert breaker_until is not None
            if breaker_until.tzinfo is None:  # sqlite returns naive datetimes
                breaker_until = breaker_until.replace(tzinfo=utcnow().tzinfo)
            assert breaker_until > utcnow()
            state = await dal.update_poll_state("acme", ok=True, watermark=utcnow())
            assert state.consecutive_failures == 0
            assert state.last_ok is not None


async def test_repo_upsert_rename_and_pinned_demotion(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.upsert_repo(
                github_id=1,
                org_login="acme",
                full_name="acme/widgets",
                stars=100,
                language="Py",
                archived=False,
                pushed_at=utcnow(),
            )
            await dal.upsert_repo(
                github_id=2,
                org_login="acme",
                full_name="acme/old",
                stars=100,
                language="Py",
                archived=False,
                pushed_at=utcnow(),
            )
            await dal.upsert_repo(
                github_id=3,
                org_login="acme",
                full_name="acme/pinned",
                stars=50,
                language="Py",
                archived=False,
                pushed_at=utcnow(),
                is_pinned=True,
            )
            await dal.upsert_repo(
                github_id=1,
                org_login="acme",
                full_name="acme/widgets2",
                stars=200,
                language="Py",
                archived=False,
                pushed_at=utcnow(),
            )
            repo = await dal.find_repo("acme/WIDGETS2")
            assert repo is not None and repo.stars == 200
            demoted = await dal.demote_missing_repos("acme", {1})
            assert demoted == 1  # old demoted; pinned repo (id 3) protected
            names = await dal.monitored_repo_names()
            assert names == {"acme/widgets2", "acme/pinned"}


async def test_setup_priors_majority(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.upsert_repo(
                github_id=9,
                org_login="acme",
                full_name="acme/mixed",
                stars=100,
                language="C",
                archived=False,
                pushed_at=utcnow(),
            )
            # below min samples -> unknown
            for _ in range(3):
                await dal.record_setup_verdict("acme/mixed", "heavy")
            assert await dal.resolve_setup_prior("acme/mixed", 5) is None
            for _ in range(2):
                await dal.record_setup_verdict("acme/mixed", "minimal")
            for _ in range(2):
                await dal.record_setup_verdict("acme/mixed", "heavy")
            assert await dal.resolve_setup_prior("acme/mixed", 5) == "heavy"
    async with db_factory() as session:
        async with session.begin():
            repo = await DAL(session).find_repo("acme/mixed")
            assert repo.setup_prior == {"minimal": 2, "moderate": 0, "heavy": 5}


async def test_majority_weight_static():
    assert DAL.majority_weight({"minimal": 1, "heavy": 2}) == "heavy"
    assert DAL.majority_weight({}) is None
    assert DAL.majority_weight({"moderate": 0}) is None


async def test_funnel_counters(db_factory):
    day = "2099-01-01"
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.bump_funnel(day, "seen")
            await dal.bump_funnel(day, "seen", 4)
            await dal.bump_funnel(day, "owner:acme:seen", 5)
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            funnel = await dal.read_funnel(day)
            assert funnel["seen"] == 5
            assert funnel["owner:acme:seen"] == 5
            assert await dal.ai_calls_today(day) == 0
            await dal.bump_funnel(day, "ai_calls")
            assert await dal.ai_calls_today(day) == 1


async def test_reconcile_watch_case_insensitive(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.upsert_org("NixOS")
            await dal.upsert_org("apache")  # retired org
            await dal.init_poll_state("apache", utcnow())
            await dal.init_poll_state("NixOS", utcnow())
            await dal.init_poll_state("pinned", utcnow())
            removed_orgs, removed_scopes = await dal.reconcile_watch({"nixos"}, {"NixOS", "pinned"})
            assert (removed_orgs, removed_scopes) == (1, 1)
            logins = {o.login for o in await dal.get_orgs()}
            assert logins == {"NixOS"}
            assert await dal.get_poll_state("apache") is None


async def test_prune(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.insert_issue(issue_values(1, first_seen_at=utcnow() - timedelta(days=120)))
            await dal.insert_issue(issue_values(2))
            await dal.insert_notification("acme/widgets#1", "telegram")
            await dal.bump_funnel("1999-01-01", "seen")
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issues, notifications, metrics = await dal.prune(
                issues_before=utcnow() - timedelta(days=90),
                notifications_before=utcnow() - timedelta(days=90),
                metrics_before_day="2000-01-01",
            )
            assert (issues, notifications, metrics) == (1, 1, 1)
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.get_issue("acme/widgets", 1) is None
            assert await dal.get_issue("acme/widgets", 2) is not None


async def test_state_refresh_selection(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.insert_issue(
                issue_values(1, score=90, notified=True, first_seen_at=utcnow() - timedelta(hours=1))
            )
            await dal.insert_issue(issue_values(2, score=10, first_seen_at=utcnow() - timedelta(hours=1)))
            await dal.insert_issue(
                issue_values(3, score=90, notified=True, first_seen_at=utcnow() - timedelta(hours=72))
            )
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issues = await dal.issues_for_state_refresh(
                first_seen_after=utcnow() - timedelta(hours=24), min_score=70, limit=10
            )
            assert [i.number for i in issues] == [1]


async def test_watchlist_summary(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.upsert_repo(
                github_id=1,
                org_login="acme",
                full_name="acme/big",
                stars=50000,
                language="Py",
                archived=False,
                pushed_at=utcnow(),
            )
            await dal.upsert_repo(
                github_id=2,
                org_login="acme",
                full_name="acme/small",
                stars=50,
                language="Py",
                archived=False,
                pushed_at=utcnow(),
                is_pinned=True,
            )
    async with db_factory() as session:
        async with session.begin():
            summary = await DAL(session).watchlist_summary()
    names = [name for name, _stars, _pin in summary["acme"]]
    assert set(names) == {"acme/big", "acme/small"}
