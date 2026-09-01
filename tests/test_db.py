"""DAL tests on SQLite (dedupe PKs, poll_state, notifications, prune, stats)."""

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
            issue = await dal.get_issue("apache/kafka", 1)
            assert issue is not None
            assert issue.issue_key == "apache/kafka#1"


async def test_notification_exactly_once(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.insert_notification("apache/kafka#1", "telegram") is True
            assert await dal.insert_notification("apache/kafka#1", "telegram") is False
            await dal.mark_notification_sent("apache/kafka#1", "telegram")
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            pending = await dal.pending_notifications()
            assert pending == []
            assert await dal.count_pending_notifications() == 0


async def test_pending_notifications_join_issue(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.insert_issue(issue_values(1, score=90, notified=True))
            await dal.insert_notification("apache/kafka#1", "telegram")
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            pending = await dal.pending_notifications()
            assert len(pending) == 1
            notification, issue = pending[0]
            assert notification.issue_key == "apache/kafka#1"
            assert issue.score == 90
            assert issue.html_url.endswith("/issues/1")


async def test_poll_state_lifecycle(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            state = await dal.init_poll_state("apache", utcnow())
            assert state.consecutive_failures == 0
            for _ in range(5):
                state = await dal.update_poll_state(
                    "apache", ok=False, breaker_failures=5, breaker_cooldown_seconds=900
                )
            assert state.consecutive_failures == 5
            breaker_until = state.breaker_until
            assert breaker_until is not None
            if breaker_until.tzinfo is None:  # sqlite returns naive datetimes
                breaker_until = breaker_until.replace(tzinfo=utcnow().tzinfo)
            assert breaker_until > utcnow()
            state = await dal.update_poll_state("apache", ok=True, watermark=utcnow())
            assert state.consecutive_failures == 0
            assert state.last_ok is not None


async def test_repo_upsert_and_demotion(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.upsert_repo(
                github_id=1,
                org_login="apache",
                full_name="apache/kafka",
                stars=100,
                language="Java",
                archived=False,
                pushed_at=utcnow(),
            )
            await dal.upsert_repo(
                github_id=2,
                org_login="apache",
                full_name="apache/old",
                stars=100,
                language="Java",
                archived=False,
                pushed_at=utcnow(),
            )
            # rename flows through the same github_id
            await dal.upsert_repo(
                github_id=1,
                org_login="apache",
                full_name="apache/kafka2",
                stars=200,
                language="Java",
                archived=False,
                pushed_at=utcnow(),
            )
            repo = await dal.find_repo("apache/KAFKA2")
            assert repo is not None and repo.stars == 200
            demoted = await dal.demote_missing_repos("apache", {1})
            assert demoted == 1
            names = await dal.monitored_repo_names()
            assert names == {"apache/kafka2"}


async def test_org_issue_stats(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.insert_issue(
                issue_values(1, labels=["good first issue"], created_at=utcnow() - timedelta(hours=3))
            )
            await dal.insert_issue(issue_values(2, labels=["bug"], created_at=utcnow() - timedelta(hours=2)))
            await dal.update_issue(
                "apache/kafka",
                2,
                {
                    "first_comment_at": utcnow() - timedelta(hours=1),
                    "created_at": utcnow() - timedelta(hours=4),
                },
            )
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            total, gfi, triage = await dal.org_issue_stats(
                "apache",
                since=utcnow() - timedelta(days=30),
                gfi_labels={"good first issue", "first-timers-only"},
            )
            assert (total, gfi) == (2, 1)
            assert triage and 2.0 <= triage[0] <= 4.5
            assert DAL.median_or_none(triage) == triage[0]
            assert DAL.median_or_none([]) is None


async def test_prune(db_factory):
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            await dal.insert_issue(issue_values(1, first_seen_at=utcnow() - timedelta(days=120)))
            await dal.insert_issue(issue_values(2))
            await dal.insert_notification("apache/kafka#1", "telegram")
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            issues, notifications = await dal.prune(
                issues_before=utcnow() - timedelta(days=90),
                notifications_before=utcnow() - timedelta(days=90),
            )
            assert (issues, notifications) == (1, 1)
    async with db_factory() as session:
        async with session.begin():
            dal = DAL(session)
            assert await dal.get_issue("apache/kafka", 1) is None
            assert await dal.get_issue("apache/kafka", 2) is not None


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
