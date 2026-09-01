"""Contribution Opportunity Score v1 (report 01prd §11) — deterministic, config-driven.

recency 25 · unassigned 20 · no linked open PR 15 · labels 15 · repo pushed 10 · stars 5
· body quality 5 → cap 100. All weights come from ScoringConfig (config.yaml).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from optyra.config import ScoringConfig
from optyra.core.filters import body_quality_points, canonical_labels
from optyra.core.normalize import ParsedIssue

SCORE_CAP = 100


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
    """Sum of matched label weights, capped at the factor ceiling (report §11: max 15)."""
    canonical = canonical_labels(labels, cfg.label_aliases)
    return min(cfg.label_points_cap, sum(cfg.labels.get(label, 0) for label in canonical))


def stars_points(stars: int, cfg: ScoringConfig) -> int:
    for min_stars, award in cfg.stars:  # descending thresholds
        if stars >= min_stars:
            return award
    return 0


def score_issue(
    issue: ParsedIssue,
    cfg: ScoringConfig,
    *,
    now: datetime | None = None,
    repo_stars: int = 0,
    repo_pushed_at: datetime | None = None,
    assigned: bool = False,
    linked_open_pr: bool = False,
) -> ScoreBreakdown:
    """Pre-deep-check pass: call with defaults for assigned/linked flags, then re-score
    after the deep-check confirms them (report §1 step 3)."""
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
    total = min(SCORE_CAP, sum(components.values()))
    return ScoreBreakdown(total=total, components=components)


def map_gsoc_score(
    *,
    gsoc_years: list[int],
    has_mega_repo: bool,
    newcomer_ratio: float | None,
    median_triage_hours: float | None,
    cfg: ScoringConfig,
    current_year: int | None = None,
) -> tuple[int, dict[str, int]]:
    """GSoC relevance score 0-100 (report §11): years 40 · mega-repo 20 ·
    newcomer-issue ratio 20 · triage-within-48h proxy 20."""
    current_year = current_year or datetime.now(UTC).year
    recent = [y for y in gsoc_years if current_year - 6 <= y <= current_year]
    years_points = 0
    for min_years, award in cfg.gsoc_years:  # descending: 6->40, 4->30, 2->20, 1->10
        if len(recent) >= min_years:
            years_points = award
            break
    mega = cfg.gsoc_mega_repo if has_mega_repo else 0
    if newcomer_ratio is None:
        newcomer = 0
    elif newcomer_ratio >= 0.15:
        newcomer = cfg.gsoc_newcomer_ratio
    elif newcomer_ratio >= 0.05:
        newcomer = cfg.gsoc_newcomer_ratio // 2
    else:
        newcomer = 0
    if median_triage_hours is None:
        triage = 0
    elif median_triage_hours <= 48:
        triage = cfg.gsoc_triage
    elif median_triage_hours <= 120:
        triage = cfg.gsoc_triage // 2
    else:
        triage = 0
    components = {
        "gsoc_years": years_points,
        "mega_repo": mega,
        "newcomer_ratio": newcomer,
        "triage": triage,
    }
    return min(SCORE_CAP, sum(components.values())), components
