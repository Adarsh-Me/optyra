"""Contribution Opportunity Score v2 (report §11 recalibrated) — deterministic, config-driven.

v2 weights for a >=10k-star universe: recency 25 · unassigned 20 · no linked open PR 15
· labels 20 · repo pushed 3 · stars 5 · body quality 5 · setup-minimal bonus 5
− parked (convention label) 8 → cap 100. The old stars/repo-activity factors were dead
weight above the star floor; the GSoC relevance score is retired from scoring (its 40-point
org-history factor doesn't discriminate within a curated mission list).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from optyra.config import ScoringConfig
from optyra.core.filters import body_quality_points, canonical_labels
from optyra.core.normalize import ParsedIssue

SCORE_CAP = 100

SIGNAL_LABELS = frozenset(
    {"good_first_issue", "first_timers_only", "help_wanted", "beginner", "easy", "starter"}
)


@dataclass
class ScoreBreakdown:
    total: int
    components: dict[str, int]


def recency_points(created_at: datetime, cfg: ScoringConfig, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    age_seconds = (now - created_at).total_seconds()
    points = 0
    for max_age, award in cfg.recency:  # ascending (30m, 2h, 6h, 24h)
        if age_seconds <= max_age and award > points:
            points = award
    return points


def label_points(labels: list[str], cfg: ScoringConfig) -> int:
    """Sum of matched label weights, capped at the factor ceiling (v2: 20)."""
    canonical = canonical_labels(labels, cfg.label_aliases)
    return min(cfg.label_points_cap, sum(cfg.labels.get(label, 0) for label in canonical))


def stars_points(stars: int, cfg: ScoringConfig) -> int:
    for min_stars, award in cfg.stars:  # descending thresholds (50k, 20k, 10k)
        if stars >= min_stars:
            return award
    return 0


def has_signal_label(labels: list[str], cfg: ScoringConfig) -> bool:
    """v2 digest-lane gate input: does the issue carry a newcomer/help-wanted signal?"""
    canonical = set(canonical_labels(labels, cfg.label_aliases))
    return bool(canonical & SIGNAL_LABELS)


def score_issue(
    issue: ParsedIssue,
    cfg: ScoringConfig,
    *,
    now: datetime | None = None,
    repo_stars: int = 0,
    repo_pushed_at: datetime | None = None,
    assigned: bool = False,
    linked_open_pr: bool = False,
    parked: bool = False,
    setup_weight: str | None = None,
) -> ScoreBreakdown:
    """Pre-deep-check pass: call with defaults for assigned/linked flags, then re-score
    after the deep-check (and AI) confirm them."""
    now = now or datetime.now(UTC)
    components: dict[str, int] = {}
    components["recency"] = recency_points(issue.created_at, cfg, now=now)
    components["unassigned"] = 0 if assigned else cfg.unassigned
    components["no_linked_pr"] = 0 if linked_open_pr else cfg.no_linked_pr
    components["labels"] = label_points(issue.labels, cfg)
    if repo_pushed_at is not None:
        if repo_pushed_at.tzinfo is None:  # sqlite (tests) returns naive datetimes
            repo_pushed_at = repo_pushed_at.replace(tzinfo=UTC)
        age_days = (now - repo_pushed_at).total_seconds() / 86400
        components["repo_activity"] = cfg.repo_pushed_days if age_days <= cfg.repo_pushed_window_days else 0
    else:
        components["repo_activity"] = 0
    components["stars"] = stars_points(repo_stars, cfg)
    components["body_quality"] = cfg.body_quality if body_quality_points(issue.body) else 0
    if parked:
        components["parked"] = -cfg.parked_penalty
    if setup_weight == "minimal":
        components["setup_minimal"] = cfg.setup_minimal_bonus
    total = max(0, min(SCORE_CAP, sum(components.values())))
    return ScoreBreakdown(total=total, components=components)
