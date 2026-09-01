"""Scoring tests — report §11 weights, caps, and GSoC mapping."""

from __future__ import annotations

from datetime import timedelta

from conftest import make_issue_item, utcnow
from optyra.core.normalize import parse_search_item
from optyra.core.scoring import map_gsoc_score, recency_points, score_issue


def _score(cfg, item, **kw):
    parsed = parse_search_item(item)
    return score_issue(parsed, cfg.scoring, now=utcnow(), **kw)


def test_full_house_instant(cfg):
    """Fresh + unassigned + no PR + good_first_issue + 28k stars pushed yesterday + repro body."""
    breakdown = _score(cfg, make_issue_item(), repo_stars=28000, repo_pushed_at=utcnow() - timedelta(days=1))
    assert breakdown.total == 95
    assert breakdown.components["recency"] == 25
    assert breakdown.components["unassigned"] == 20
    assert breakdown.components["no_linked_pr"] == 15
    assert breakdown.components["labels"] == 15
    assert breakdown.components["repo_activity"] == 10
    assert breakdown.components["stars"] == 5
    assert breakdown.components["body_quality"] == 5


def test_below_digest_threshold(cfg):
    """Old, plain bug label, low stars: 6h band(12) + 20 + 15 + 3 + 0 + 0 + 5 = 55."""
    breakdown = _score(
        cfg,
        make_issue_item(labels=("bug",), created_min_ago=300),
        repo_stars=1000,
    )
    assert breakdown.total == 55
    assert breakdown.total < cfg.notify.digest_threshold


def test_assigned_and_linked_pr_remove_points(cfg):
    item = make_issue_item()
    full = _score(cfg, item, repo_stars=28000)
    claimed = _score(cfg, item, repo_stars=28000, assigned=True, linked_open_pr=True)
    assert claimed.total == full.total - cfg.scoring.unassigned - cfg.scoring.no_linked_pr


def test_score_capped_at_100(cfg):
    breakdown = _score(cfg, make_issue_item(), repo_stars=28000)
    assert breakdown.total <= 100


def test_recency_bands(cfg):
    from optyra.core.normalize import parse_search_item

    cases = [(29, 25), (61, 20), (5 * 60, 12), (23 * 60, 6), (25 * 60, 0)]
    for minutes, expected in cases:
        parsed = parse_search_item(make_issue_item(created_min_ago=minutes))
        assert recency_points(parsed.created_at, cfg.scoring) == expected, minutes


def test_label_alias_mapping(cfg):
    item = make_issue_item(labels=("good first issue 🌱",))
    assert _score(cfg, item).components["labels"] == 15
    item = make_issue_item(labels=("up-for-grabs",))
    assert _score(cfg, item).components["labels"] == 5


def test_label_points_capped_at_15(cfg):
    item = make_issue_item(labels=("good first issue", "help wanted", "bug", "enhancement"))
    assert _score(cfg, item).components["labels"] == 15


def test_stars_tiers(cfg):
    item = make_issue_item()
    for stars, expected in ((11000, 5), (6000, 3), (2500, 2), (1000, 0)):
        assert _score(cfg, item, repo_stars=stars).components["stars"] == expected


def test_gsoc_years_mapping(cfg):
    base = dict(has_mega_repo=False, newcomer_ratio=None, median_triage_hours=None, cfg=cfg.scoring)
    for years, expected in ((6, 40), (5, 30), (4, 30), (3, 20), (2, 20), (1, 10), (0, 0)):
        score, _ = map_gsoc_score(gsoc_years=list(range(2026 - years, 2026)), current_year=2026, **base)
        assert score == expected, years


def test_gsoc_full_components(cfg):
    score, components = map_gsoc_score(
        gsoc_years=[2020, 2021, 2022, 2023, 2024, 2025],
        has_mega_repo=True,
        newcomer_ratio=0.25,
        median_triage_hours=20.0,
        cfg=cfg.scoring,
        current_year=2026,
    )
    assert components == {"gsoc_years": 40, "mega_repo": 20, "newcomer_ratio": 20, "triage": 20}
    assert score == 100


def test_gsoc_insufficient_data_scores_zero(cfg):
    score, components = map_gsoc_score(
        gsoc_years=[],
        has_mega_repo=False,
        newcomer_ratio=None,  # <20 issues in 30d -> no trustworthy ratio
        median_triage_hours=None,  # no timeline samples yet
        cfg=cfg.scoring,
        current_year=2026,
    )
    assert score == 0 and components["newcomer_ratio"] == 0 and components["triage"] == 0


def test_gsoc_partial_ratio_and_slow_triage(cfg):
    score, components = map_gsoc_score(
        gsoc_years=[2025],
        has_mega_repo=False,
        newcomer_ratio=0.10,
        median_triage_hours=90.0,
        cfg=cfg.scoring,
        current_year=2026,
    )
    assert components == {"gsoc_years": 10, "mega_repo": 0, "newcomer_ratio": 10, "triage": 10}
    assert score == 30
