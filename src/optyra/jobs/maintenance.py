"""Digest flush + pruning maintenance jobs (report 01prd §9, §6).

DigestFlushJob: every digest_interval, all pending notifications (instant sends that
failed self-heal into this path) are batched into one ranked Telegram message. Items are
marked sent only for chunks that were actually delivered.

MaintenanceJob: prunes issues + notifications older than prune_after_days.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from optyra.db.dal import DAL, utcnow_aware
from optyra.notify.telegram import build_digest
from optyra.services import Services

logger = logging.getLogger(__name__)


def _issue_ctx(issue) -> dict:
    return {
        "issue_key": issue.issue_key,
        "html_url": issue.html_url,
        "title": issue.title,
        "score": issue.score,
        "labels": (issue.labels or [])[:4],
        "stars": None,
        "gsoc_score": issue.gsoc_score,
        "ai_summary": issue.ai_summary,
        "ai_worth_attempting": issue.ai_worth_attempting,
        "ai_reason_codes": issue.ai_reason_codes or [],
        "ai_difficulty": issue.ai_difficulty,
    }


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
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                pending = await dal.pending_notifications()
        if not pending:
            return 0
        items = [_issue_ctx(issue) for _, issue in pending]
        chunks = build_digest(items)
        delivered = 0
        sent_keys: set[str] = set()
        for text, chunk_items in chunks:
            if await self.svc.tg.send_message(text):
                delivered += len(chunk_items)
                sent_keys.update(item["issue_key"] for item in chunk_items)
        if sent_keys:
            async with self.svc.session_factory() as session:
                async with session.begin():
                    dal = DAL(session)
                    for issue_key in sent_keys:
                        await dal.mark_notification_sent(issue_key, "telegram")
        self.svc.health.last_digest_flush_at = utcnow_aware().isoformat(timespec="seconds")
        self.svc.health.pending_notifications = len(pending) - delivered
        return delivered


class MaintenanceJob:
    def __init__(self, services: Services) -> None:
        self.svc = services
        self.cfg = services.cfg

    async def run_forever(self) -> None:
        while True:
            started = utcnow_aware()
            try:
                issues, notifications = await self.prune_once()
                if issues or notifications:
                    logger.info("pruned %s issues, %s notifications", issues, notifications)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("maintenance prune failed; continuing")
            elapsed = (utcnow_aware() - started).total_seconds()
            await asyncio.sleep(max(60.0, self.cfg.maintenance.prune_interval_seconds - elapsed))

    async def prune_once(self) -> tuple[int, int]:
        cutoff = utcnow_aware() - timedelta(days=self.cfg.maintenance.prune_after_days)
        async with self.svc.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                return await dal.prune(issues_before=cutoff, notifications_before=cutoff)
