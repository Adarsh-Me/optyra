"""Shared candidate-processing context for poll → deep-check → enrich → notify.

One code path so the poll job and the catch-up path behave identically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from optyra.ai.enricher import Enrichment
from optyra.core import filters as filters_mod
from optyra.core.normalize import ParsedIssue
from optyra.core.scoring import ScoreBreakdown, score_issue
from optyra.db.dal import DAL
from optyra.github.client import GitHubClient
from optyra.notify.telegram import format_instant
from optyra.services import Services

logger = logging.getLogger(__name__)

CHANNEL = "telegram"  # one channel per delivery medium; instant vs digest is a mode


@dataclass
class CandidateOutcome:
    repo_full_name: str
    number: int
    issue_key: str
    score: int
    notified_instant: bool
    queued_digest: bool
    ai_summary: str | None


class CandidatePipeline:
    def __init__(self, services: Services) -> None:
        self.svc = services
        self.cfg = services.cfg

    # ------------------------------------------------------------------ scoring helpers

    def _repo_kwargs(self, repo_row) -> dict:
        return {
            "repo_stars": int(repo_row.stars or 0) if repo_row else 0,
            "repo_pushed_at": repo_row.pushed_at if repo_row else None,
        }

    def _issue_ctx(
        self,
        issue: ParsedIssue,
        breakdown: ScoreBreakdown,
        enrichment: Enrichment | None,
        *,
        stars: int,
        language: str | None,
        gsoc_score: int,
    ) -> dict:
        return {
            "issue_key": issue.issue_key,
            "html_url": issue.html_url,
            "title": issue.title,
            "score": breakdown.total,
            "labels": issue.labels[:4],
            "stars": stars,
            "gsoc_score": gsoc_score,
            "ai_summary": enrichment.summary if enrichment else None,
            "ai_worth_attempting": enrichment.worth_attempting if enrichment else None,
            "ai_reason_codes": enrichment.reason_codes if enrichment else [],
            "ai_difficulty": enrichment.difficulty if enrichment else None,
        }

    # ------------------------------------------------------------------ main path

    async def process_new_issue(
        self,
        dal: DAL,
        parsed: ParsedIssue,
        *,
        repo_row,
        now: datetime,
    ) -> CandidateOutcome:
        """A brand-new issue row (fresh insert). Filter → score → deep-check → enrich → notify.

        Returns the outcome; zero exceptions expected (all handled internally).
        """
        outcome = CandidateOutcome(
            repo_full_name=parsed.repo_full_name,
            number=parsed.number,
            issue_key=parsed.issue_key,
            score=0,
            notified_instant=False,
            queued_digest=False,
            ai_summary=None,
        )
        fcfg = self.cfg.filters
        result = filters_mod.hard_filter(parsed, fcfg, now=now, repo_monitored=True)
        if not result.ok:
            await dal.update_issue(
                parsed.repo_full_name,
                parsed.number,
                {"filtered_reason": result.reason, "last_checked_at": now},
            )
            return outcome

        breakdown = score_issue(parsed, self.cfg.scoring, now=now, **self._repo_kwargs(repo_row))
        if breakdown.total < self.cfg.notify.digest_threshold:
            await dal.update_issue(
                parsed.repo_full_name,
                parsed.number,
                {"score": breakdown.total, "last_checked_at": now, "filtered_reason": "below-threshold"},
            )
            return outcome

        # ---- deep-check (report §1 step 3): confirm assignee + linked PR + freshness
        deep = await self._deep_check(parsed)
        if deep.get("gone"):
            await dal.update_issue(
                parsed.repo_full_name,
                parsed.number,
                {"state": "closed", "last_checked_at": now, "filtered_reason": "closed-at-deepcheck"},
            )
            return outcome
        if deep.get("assigned"):
            await dal.update_issue(
                parsed.repo_full_name,
                parsed.number,
                {
                    "assignees": deep.get("assignees") or [],
                    "score": 0,
                    "filtered_reason": "assigned-at-deepcheck",
                    "last_checked_at": now,
                },
            )
            return outcome
        linked_open_pr = bool(deep.get("linked_open_pr"))
        await dal.update_issue(
            parsed.repo_full_name,
            parsed.number,
            {
                "linked_pr": bool(deep.get("linked_pr")),
                "linked_open_pr": linked_open_pr,
                "first_comment_at": deep.get("first_comment_at"),
            },
        )

        # ---- final score with deep-check facts
        breakdown = score_issue(
            parsed,
            self.cfg.scoring,
            now=now,
            assigned=False,
            linked_open_pr=linked_open_pr,
            **self._repo_kwargs(repo_row),
        )
        if breakdown.total < self.cfg.notify.digest_threshold:
            await dal.update_issue(
                parsed.repo_full_name,
                parsed.number,
                {
                    "score": breakdown.total,
                    "last_checked_at": now,
                    "filtered_reason": "below-threshold-post-deepcheck",
                },
            )
            return outcome

        # ---- AI enrichment: candidates only, inline, fail-open (02prd §4)
        enrichment = None
        if self.svc.enricher is not None:
            enrichment = await self.svc.enricher.enrich(
                {
                    "repo": parsed.repo_full_name,
                    "stars": int(repo_row.stars or 0) if repo_row else 0,
                    "language": repo_row.language if repo_row else None,
                    "labels": parsed.labels,
                    "assignee": None,
                    "linked_pr": linked_open_pr,
                    "title": parsed.title,
                    "body": parsed.body,
                }
            )
        ai_values = {
            "ai_summary": enrichment.summary if enrichment else None,
            "ai_worth_attempting": enrichment.worth_attempting if enrichment else None,
            "ai_difficulty": enrichment.difficulty if enrichment else None,
            "ai_reason_codes": enrichment.reason_codes if enrichment else [],
            "ai_enriched_at": now if enrichment else None,
            "ai_model": self.svc.enricher.model if enrichment else None,
        }

        gsoc = await dal.org_gsoc_score(parsed.repo_full_name.split("/")[0])
        await dal.update_issue(
            parsed.repo_full_name,
            parsed.number,
            {
                "score": breakdown.total,
                "gsoc_score": gsoc,
                "last_checked_at": now,
                **ai_values,
            },
        )

        # ---- notify: insert-then-send on the (issue_key, channel) PK (report §16)
        issue_ctx = self._issue_ctx(
            parsed,
            breakdown,
            enrichment,
            stars=int(repo_row.stars or 0) if repo_row else 0,
            language=repo_row.language if repo_row else None,
            gsoc_score=gsoc,
        )
        if self.svc.tg is None:
            outcome.score = breakdown.total
            outcome.ai_summary = enrichment.summary if enrichment else None
            return outcome

        claimed = await dal.insert_notification(parsed.issue_key, CHANNEL)
        if not claimed:
            return outcome  # someone else notified — exactly-once holds
        await dal.update_issue(parsed.repo_full_name, parsed.number, {"notified": True})

        if breakdown.total >= self.cfg.notify.instant_threshold:
            sent = await self.svc.tg.send_message(format_instant(issue_ctx), button_url=parsed.html_url)
            if sent:
                await dal.mark_notification_sent(parsed.issue_key, CHANNEL)
                outcome.notified_instant = True
            else:
                # leave unsent: the digest flush retries it (self-healing)
                outcome.queued_digest = True
        else:
            outcome.queued_digest = True  # pending row flushed by the digest job
        outcome.score = breakdown.total
        outcome.ai_summary = enrichment.summary if enrichment else None
        return outcome

    # ------------------------------------------------------------------ deep-check

    async def _deep_check(self, parsed: ParsedIssue) -> dict:
        """REST confirmations for candidates only (report §10): fresh issue fetch +
        timeline cross-references. Every failure degrades gracefully."""
        out: dict = {
            "gone": False,
            "assigned": False,
            "assignees": [],
            "linked_pr": False,
            "linked_open_pr": False,
            "first_comment_at": None,
        }
        gh: GitHubClient = self.svc.gh
        try:
            fresh = await gh.get_issue(parsed.repo_full_name, parsed.number)
        except Exception as exc:
            if getattr(exc, "status", None) in (404, 410):
                out["gone"] = True
                return out
            logger.warning("deep-check issue fetch failed for %s: %r", parsed.issue_key, exc)
            return out
        state = str(fresh.get("state") or "open")
        if state != "open":
            out["gone"] = True
            return out
        assignees = [
            str(a.get("login"))
            for a in (fresh.get("assignees") or [])
            if isinstance(a, dict) and a.get("login")
        ]
        out["assignees"] = assignees
        out["assigned"] = bool(assignees)
        try:
            timeline = await gh.get_issue_timeline(parsed.repo_full_name, parsed.number)
        except Exception as exc:
            logger.warning("deep-check timeline failed for %s: %r", parsed.issue_key, exc)
            return out
        from datetime import datetime as _dt

        first_comment = None
        for event in timeline:
            event_type = event.get("event")
            if event_type == "cross-referenced":
                source = event.get("source") or {}
                source_issue = source.get("issue") or {}
                if "pull_request" in source_issue:
                    out["linked_pr"] = True
                    pr_state = str(source_issue.get("state") or "open")
                    pr_draft = bool((source_issue.get("pull_request") or {}).get("draft"))
                    if pr_state == "open" and not pr_draft:
                        out["linked_open_pr"] = True
            elif event_type == "commented" and first_comment is None:
                created = event.get("created_at")
                if isinstance(created, str):
                    try:
                        first_comment = _dt.fromisoformat(created.replace("Z", "+00:00"))
                    except ValueError:
                        pass
        out["first_comment_at"] = first_comment
        return out
