"""Candidate pipeline: poll -> hard filter -> score -> deep-check -> enrich/setup-policy ->
notify. v2 adds the setup-weight policy (drop/demote heavy), the digest lane gate
(newcomer signal required), the per-owner daily budget accounting, and funnel counters.

One code path so the org scopes and the pinned-repo scope behave identically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from optyra.ai.enricher import Enrichment
from optyra.core import filters as filters_mod
from optyra.core import scoring as scoring_mod
from optyra.core.normalize import ParsedIssue
from optyra.core.scoring import ScoreBreakdown
from optyra.db.dal import DAL
from optyra.github.client import GitHubClient
from optyra.notify.telegram import format_instant
from optyra.services import Services

logger = logging.getLogger(__name__)

CHANNEL = "telegram"  # one channel per delivery medium; instant vs digest is a mode
SETUP_DROP_SCORE = 49  # a "heavy" hard verdict must never look instant-eligible

# Funnel stage keys (v2 daily self-report): seen, gate_*, hard_filter, below_threshold,
# deep_rejected, setup_dropped, lane_gate, ai_*, notified_instant, notified_digest,
# budget_suppressed (applied at flush time), owner:<login>:seen/notified.


@dataclass
class CandidateOutcome:
    repo_full_name: str
    number: int
    issue_key: str
    score: int
    notified_instant: bool
    queued_digest: bool
    dropped: str | None  # funnel stage key when not notified
    setup_weight: str | None = None
    ai_summary: str | None = None


def owner_of(repo_full_name: str) -> str:
    return repo_full_name.split("/")[0].lower()


class CandidatePipeline:
    def __init__(self, services: Services) -> None:
        self.svc = services
        self.cfg = services.cfg

    async def bump(self, dal: DAL, key: str) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        await dal.bump_funnel(day, key)

    async def _bump_owner(self, dal: DAL, repo_full_name: str, kind: str) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        await dal.bump_funnel(day, f"owner:{owner_of(repo_full_name)}:{kind}")

    # ------------------------------------------------------------------ helpers

    def _setup_policy(self, repo_full_name: str) -> str:
        owner = repo_full_name.split("/")[0].lower()
        return self.cfg.ai.org_setup_filter.get(owner, self.cfg.ai.setup_filter_default)

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
        repo_row,
        parked: str | None,
    ) -> dict:
        return {
            "issue_key": issue.issue_key,
            "html_url": issue.html_url,
            "title": issue.title,
            "score": breakdown.total,
            "labels": issue.labels[:4],
            "stars": int(repo_row.stars or 0) if repo_row else 0,
            "parked": parked,
            "ai_summary": enrichment.summary if enrichment else None,
            "ai_worth_attempting": enrichment.worth_attempting if enrichment else None,
            "ai_reason_codes": enrichment.reason_codes if enrichment else [],
            "ai_difficulty": enrichment.difficulty if enrichment else None,
            "ai_setup_weight": enrichment.setup_weight if enrichment else None,
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
        """A brand-new issue row (fresh insert). Filter -> score -> deep-check -> enrich ->
        setup policy -> lane gate -> notify. Never raises: all failures degrade to drops."""
        outcome = CandidateOutcome(
            repo_full_name=parsed.repo_full_name,
            number=parsed.number,
            issue_key=parsed.issue_key,
            score=0,
            notified_instant=False,
            queued_digest=False,
            dropped=None,
        )
        await self._bump_owner(dal, parsed.repo_full_name, "seen")

        parked = filters_mod.parked_hit(parsed, self.cfg.filters)

        result = filters_mod.hard_filter(parsed, self.cfg.filters, now=now, repo_monitored=True)
        if not result.ok:
            await dal.update_issue(
                parsed.repo_full_name,
                parsed.number,
                {"filtered_reason": result.reason, "last_checked_at": now},
            )
            await self.bump(dal, "hard_filter")
            outcome.dropped = result.reason
            return outcome

        breakdown = scoring_mod.score_issue(
            parsed, self.cfg.scoring, now=now, parked=parked is not None, **self._repo_kwargs(repo_row)
        )
        if breakdown.total < self.cfg.notify.digest_threshold:
            await dal.update_issue(
                parsed.repo_full_name,
                parsed.number,
                {"score": breakdown.total, "last_checked_at": now, "filtered_reason": "below-threshold"},
            )
            await self.bump(dal, "below_threshold")
            outcome.dropped = "below-threshold"
            return outcome

        # ---- deep-check (report §1 step 3): confirm assignee + linked PR + freshness
        deep = await self._deep_check(parsed)
        if deep.get("gone"):
            await dal.update_issue(
                parsed.repo_full_name,
                parsed.number,
                {"state": "closed", "last_checked_at": now, "filtered_reason": "closed-at-deepcheck"},
            )
            await self.bump(dal, "deep_rejected")
            outcome.dropped = "closed-at-deepcheck"
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
            await self.bump(dal, "deep_rejected")
            outcome.dropped = "assigned-at-deepcheck"
            return outcome
        linked_open_pr = bool(deep.get("linked_open_pr"))
        await dal.update_issue(
            parsed.repo_full_name,
            parsed.number,
            {
                "linked_pr": bool(deep.get("linked_pr")),
                "linked_open_pr": linked_open_pr,
                "first_comment_at": deep.get("first_comment_at"),
                "parked": parked is not None,
            },
        )

        # ---- AI enrichment: candidates only, inline, fail-open. v2: hard daily call cap;
        # when the model is down we fall back to the per-repo prior (default: allow).
        enrichment = None
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        if self.svc.enricher is not None and await dal.ai_calls_today(day) < self.cfg.ai.daily_call_cap:
            await self.bump(dal, "ai_calls")
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
            if enrichment is None:
                await self.bump(dal, "ai_failures")

        setup_weight = enrichment.setup_weight if enrichment else None
        if setup_weight is None and self.svc.enricher is None:
            # AI disabled entirely: no gating, pure rule engine (fail-open by design).
            pass
        elif setup_weight is None:
            setup_weight = await dal.resolve_setup_prior(parsed.repo_full_name, self.cfg.ai.prior_min_samples)
            if setup_weight is None and self.cfg.ai.keyword_screen:
                probe = f"{parsed.title} {parsed.body}".lower()
                if any(k in probe for k in self.cfg.ai.setup_keywords):
                    setup_weight = "heavy"

        # ---- final score with deep-check facts + setup bonus
        breakdown = scoring_mod.score_issue(
            parsed,
            self.cfg.scoring,
            now=now,
            assigned=False,
            linked_open_pr=linked_open_pr,
            parked=parked is not None,
            setup_weight=setup_weight,
            **self._repo_kwargs(repo_row),
        )

        # ---- setup policy: heavy -> drop (hard) or digest-only demotion (soft)
        policy = self._setup_policy(parsed.repo_full_name)
        if setup_weight == "heavy":
            if policy == "hard":
                await dal.update_issue(
                    parsed.repo_full_name,
                    parsed.number,
                    {
                        "score": min(breakdown.total, SETUP_DROP_SCORE),
                        "setup_weight": setup_weight,
                        "last_checked_at": now,
                        "filtered_reason": "setup-heavy",
                    },
                )
                await dal.record_setup_verdict(parsed.repo_full_name, "heavy")
                await self.bump(dal, "setup_dropped")
                outcome.dropped = "setup-heavy"
                return outcome
            # soft: cap below the instant lane -> digest-only, annotated ⚠️ in message
            breakdown = ScoreBreakdown(
                total=min(breakdown.total, self.cfg.notify.instant_threshold - 1),
                components={**breakdown.components, "setup_soft_cap": 0},
            )
        if enrichment is not None:
            await dal.record_setup_verdict(parsed.repo_full_name, enrichment.setup_weight)

        if breakdown.total < self.cfg.notify.digest_threshold:
            await dal.update_issue(
                parsed.repo_full_name,
                parsed.number,
                {
                    "score": breakdown.total,
                    "setup_weight": setup_weight,
                    "last_checked_at": now,
                    "filtered_reason": "below-threshold-post-deepcheck",
                },
            )
            await self.bump(dal, "below_threshold")
            outcome.dropped = "below-threshold"
            return outcome

        # ---- lane gate: the 70..84 digest lane requires a real contribution signal;
        # the instant lane may bypass it (config instant_lane_bypasses_gate).
        if self.cfg.notify.lane_signal_required:
            is_instant = breakdown.total >= self.cfg.notify.instant_threshold
            if not (is_instant and self.cfg.notify.instant_lane_bypasses_gate):
                signal = scoring_mod.has_signal_label(parsed.labels, self.cfg.scoring)
                worth = enrichment.worth_attempting if enrichment else None
                if not signal and worth is not True:
                    await dal.update_issue(
                        parsed.repo_full_name,
                        parsed.number,
                        {
                            "score": breakdown.total,
                            "setup_weight": setup_weight,
                            "last_checked_at": now,
                            "filtered_reason": "lane-gate-no-signal",
                        },
                    )
                    await self.bump(dal, "lane_gate")
                    outcome.dropped = "lane-gate"
                    return outcome

        # ---- persist + notify
        ai_values = {
            "ai_summary": enrichment.summary if enrichment else None,
            "ai_worth_attempting": enrichment.worth_attempting if enrichment else None,
            "ai_difficulty": enrichment.difficulty if enrichment else None,
            "ai_reason_codes": enrichment.reason_codes if enrichment else [],
            "ai_enriched_at": now if enrichment else None,
            "ai_model": self.svc.enricher.model if enrichment else None,
            "setup_weight": setup_weight,
        }
        await dal.update_issue(
            parsed.repo_full_name,
            parsed.number,
            {
                "score": breakdown.total,
                "last_checked_at": now,
                **ai_values,
            },
        )
        outcome.score = breakdown.total
        outcome.setup_weight = setup_weight
        outcome.ai_summary = enrichment.summary if enrichment else None

        issue_ctx = self._issue_ctx(parsed, breakdown, enrichment, repo_row=repo_row, parked=parked)
        if self.svc.tg is None:
            return outcome  # notifications off: scored/enriched rows stay DB-only

        claimed = await dal.insert_notification(parsed.issue_key, CHANNEL)
        if not claimed:
            return outcome  # someone else notified — exactly-once holds
        await dal.update_issue(parsed.repo_full_name, parsed.number, {"notified": True})

        if breakdown.total >= self.cfg.notify.instant_threshold:
            sent = await self.svc.tg.send_message(format_instant(issue_ctx), button_url=parsed.html_url)
            if sent:
                await dal.mark_notification_sent(parsed.issue_key, CHANNEL)
                await self.bump(dal, "notified_instant")
                await self._bump_owner(dal, parsed.repo_full_name, "notified")
                outcome.notified_instant = True
            else:
                # leave unsent: the digest flush retries it (self-healing)
                outcome.queued_digest = True
        else:
            await self.bump(dal, "queued_digest")
            outcome.queued_digest = True  # pending row flushed by the digest job
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
                        first_comment = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    except ValueError:
                        pass
        out["first_comment_at"] = first_comment
        return out
