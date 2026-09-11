"""Digest flush + maintenance jobs — v2.

DigestFlushJob: every digest_interval, pending notifications are converted into ONE
ranked digest, but with v2 flood defenses applied AT FLUSH TIME (never at detection,
so the best item of the day wins the slot regardless of arrival order):

  * per-owner daily budget (default 5): already-sent-today rows are counted, the
    remaining budget is filled best-first; over-budget items are SUPPRESSED (terminal,
    counted in the funnel report — never silently dropped);
  * the instant lane (score >= instant_threshold) is exempt from the owner budget;
  * per-flush size cap (digest_max_items): overflow suppressed with a footer note.

MaintenanceJob: daily prune (>90 days) and the daily funnel self-report at the
configured UTC hour.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from optyra.db.dal import DAL, utcnow_aware
from optyra.jobs.candidate import CHANNEL
from optyra.notify.telegram import build_digest, format_funnel_report
from optyra.services import Services

logger = logging.getLogger(__name__)

_FLUSH_TICK_SECONDS = 300.0


def _issue_ctx(issue) -> dict:
    return {
        "issue_key": issue.issue_key,
        "html_url": issue.html_url,
        "title": issue.title,
        "score": issue.score,
        "labels": (issue.labels or [])[:4],
        "stars": None,
        "parked": "parked" if issue.parked else None,
        "ai_summary": issue.ai_summary,
        "ai_worth_attempting": issue.ai_worth_attempting,
        "ai_reason_codes": issue.ai_reason_codes or [],
        "ai_difficulty": issue.ai_difficulty,
        "ai_setup_weight": issue.setup_weight,
    }


def _owner_of(issue_key: str) -> str:
    return issue_key.split("/")[0].lower()


class DigestFlushJob:
    def __init__(self, services: Services) -> None:
        self.svc = services
        self.cfg = services.cfg

    async def run_forever(self) -> None:
        while True:
            started = utcnow_aware()
            try:
                flushed = await self.flush_once()
                if flushed:
                    logger.info("digest flush: %s issue(s) delivered", flushed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("digest flush failed; continuing")
            elapsed = (utcnow_aware() - started).total_seconds()
            await asyncio.sleep(max(30.0, self.cfg.notify.digest_interval_seconds - elapsed))

    async def flush_once(self) -> int:
        if self.svc.tg is None:
            return 0
        now = utcnow_aware()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                pending = await dal.pending_notifications()
                sent_today = await dal.owner_digest_counts(
                    day_start, below_score=self.cfg.notify.instant_threshold
                )
        if not pending:
            return 0

        instant_threshold = self.cfg.notify.instant_threshold
        digest_items: list[dict] = []
        send_keys: list[str] = []
        suppressed_keys: list[str] = []
        owner_budget = dict(sent_today)
        per_owner = self.cfg.notify.per_owner_daily_cap
        size_cap = self.cfg.notify.digest_max_items

        for _notification, issue in pending:  # already ranked score desc
            ctx = _issue_ctx(issue)
            is_instant_lane = ctx["score"] >= instant_threshold
            owner = _owner_of(ctx["issue_key"])
            if not is_instant_lane:
                if owner_budget.get(owner, 0) >= per_owner:
                    suppressed_keys.append(ctx["issue_key"])
                    continue
            if len(digest_items) >= size_cap:
                suppressed_keys.append(ctx["issue_key"])
                continue
            digest_items.append(ctx)
            send_keys.append(ctx["issue_key"])
            if not is_instant_lane:
                owner_budget[owner] = owner_budget.get(owner, 0) + 1

        suppressed_note = len(suppressed_keys)
        chunks = build_digest(digest_items, suppressed_note=suppressed_note)
        delivered_keys: set[str] = set()
        for text, chunk_items in chunks:
            if await self.svc.tg.send_message(text):
                delivered_keys.update(item["issue_key"] for item in chunk_items)

        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                day = now.strftime("%Y-%m-%d")
                for issue_key in delivered_keys:
                    await dal.mark_notification_sent(issue_key, CHANNEL)
                for issue_key in suppressed_keys:
                    await dal.mark_notification_suppressed(issue_key, CHANNEL)
                if delivered_keys:
                    await dal.bump_funnel(day, "notified_digest", len(delivered_keys))
                if suppressed_keys:
                    await dal.bump_funnel(day, "budget_suppressed", len(suppressed_keys))
        self.svc.health.last_digest_flush_at = now.isoformat(timespec="seconds")
        self.svc.health.pending_notifications = len(pending) - len(delivered_keys) - len(suppressed_keys)
        return len(delivered_keys)


class MaintenanceJob:
    """Daily prune + hourly funnel-report check."""

    def __init__(self, services: Services) -> None:
        self.svc = services
        self.cfg = services.cfg
        self._last_prune = datetime(1970, 1, 1, tzinfo=UTC)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.prune_if_due()
                await self.maybe_send_daily_report()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("maintenance failed; continuing")
            await asyncio.sleep(_FLUSH_TICK_SECONDS)

    async def prune_if_due(self) -> None:
        if (utcnow_aware() - self._last_prune).total_seconds() < self.cfg.maintenance.prune_interval_seconds:
            return
        self._last_prune = utcnow_aware()
        cutoff = utcnow_aware() - timedelta(days=self.cfg.maintenance.prune_after_days)
        metrics_before_day = (
            utcnow_aware() - timedelta(days=self.cfg.maintenance.metrics_keep_days)
        ).strftime("%Y-%m-%d")
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                issues, notifications, metrics = await dal.prune(
                    issues_before=cutoff, notifications_before=cutoff, metrics_before_day=metrics_before_day
                )
        if issues or notifications:
            logger.info("pruned %s issues, %s notifications, %s metrics rows", issues, notifications, metrics)

    async def maybe_send_daily_report(self) -> None:
        if self.svc.tg is None or not self.cfg.funnel.report_enabled:
            return
        now = utcnow_aware()
        if now.hour != self.cfg.funnel.report_hour_utc:
            return
        day = now.strftime("%Y-%m-%d")
        sent_key = f"funnel_report_sent:{day}"
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                funnel = await dal.read_funnel(day)
                if funnel.get(sent_key):
                    return
                report = format_funnel_report(day, funnel, config_hash=self.cfg.config_hash)
                if not await self.svc.tg.send_message(report):
                    return
                await dal.bump_funnel(day, sent_key)  # terminal marker for the day
