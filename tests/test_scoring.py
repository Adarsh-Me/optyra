"""Scoring tests — v2 recalibrated weights, caps, parked penalty, setup bonus."""

from __future__ import annotations

from datetime import timedelta

from conftest import make_issue_item, utcnow
from optyra.core.normalize import parse_search_item
from optyra.core.scoring import has_signal_label, recency_points, score_issue


def _score(cfg, item, **kw):
    parsed = parse_search_item(item)
    return score_issue(parsed, cfg.scoring, now=utcnow(), **kw)


def test_full_house_instant(cfg):
    """25 fresh + 20 unassigned + 15 no-PR + 20 gfi + 3 active + 4 stars(28k) + 5 body = 92."""
    breakdown = _score(cfg, make_issue_item(), repo_stars=28000, repo_pushed_at=utcnow() - timedelta(days=1))
    assert breakdown.total == 92
    assert breakdown.components["recency"] == 25
    assert breakdown.components["unassigned"] == 20
    assert breakdown.components["no_linked_pr"] == 15
    assert breakdown.components["labels"] == 20
    assert breakdown.components["repo_activity"] == 3
    assert breakdown.components["stars"] == 4
    assert breakdown.components["body_quality"] == 5


def test_setup_minimal_bonus(cfg):
    pushed = utcnow() - timedelta(days=1)
    base = _score(cfg, make_issue_item(), repo_stars=28000, repo_pushed_at=pushed)
    minimal = _score(cfg, make_issue_item(), repo_stars=28000, repo_pushed_at=pushed, setup_weight="minimal")
    assert minimal.total == base.total + cfg.scoring.setup_minimal_bonus == 97
    moderate = _score(
        cfg, make_issue_item(), repo_stars=28000, repo_pushed_at=pushed, setup_weight="moderate"
    )
    assert moderate.total == base.total


def test_parked_penalty(cfg):
    pushed = utcnow() - timedelta(days=1)
    base = _score(cfg, make_issue_item(), repo_stars=28000, repo_pushed_at=pushed)
    parked = _score(cfg, make_issue_item(), repo_stars=28000, repo_pushed_at=pushed, parked=True)
    assert parked.total == base.total - cfg.scoring.parked_penalty == 84
    assert parked.components["parked"] == -8


def test_parked_cannot_go_negative(cfg):
    item = make_issue_item(labels=("bug",), created_min_ago=40 * 60, body="short")
    breakdown = _score(cfg, item, parked=True)
    assert breakdown.total >= 0


def test_below_digest_threshold(cfg):
    """5h old, plain bug label, sub-10k stars (pinned edge): 12+20+15+3+0+0+5 = 55."""
    breakdown = _score(cfg, make_issue_item(labels=("bug",), created_min_ago=300), repo_stars=1000)
    assert breakdown.total == 55
    assert breakdown.total < cfg.notify.digest_threshold


def test_assigned_and_linked_pr_remove_points(cfg):
    item = make_issue_item()
    full = _score(cfg, item, repo_stars=28000)
    claimed = _score(cfg, item, repo_stars=28000, assigned=True, linked_open_pr=True)
    assert claimed.total == full.total - cfg.scoring.unassigned - cfg.scoring.no_linked_pr


def test_score_max_is_98(cfg):
    breakdown = _score(
        cfg,
        make_issue_item(),
        repo_stars=60000,
        repo_pushed_at=utcnow() - timedelta(days=1),
        setup_weight="minimal",
    )
    assert breakdown.total == 98  # 25+20+15+20+3+5+5+5 — under the cap by design


def test_recency_bands(cfg):
    cases = [(29, 25), (61, 20), (5 * 60, 12), (23 * 60, 6), (25 * 60, 0)]
    for minutes, expected in cases:
        parsed = parse_search_item(make_issue_item(created_min_ago=minutes))
        assert recency_points(parsed.created_at, cfg.scoring) == expected, minutes


def test_label_alias_mapping(cfg):
    item = make_issue_item(labels=("good first issue 🌱",))
    assert _score(cfg, item).components["labels"] == 20
    item = make_issue_item(labels=("up-for-grabs",))
    assert _score(cfg, item).components["labels"] == 6


def test_label_points_capped_at_20(cfg):
    item = make_issue_item(labels=("good first issue", "help wanted", "bug", "enhancement"))
    assert _score(cfg, item).components["labels"] == 20  # 20+12+3+3=38 -> cap


def test_stars_tiers_v2(cfg):
    item = make_issue_item()
    for stars, expected in ((51000, 5), (25000, 4), (12000, 3), (9000, 0)):
        assert _score(cfg, item, repo_stars=stars).components["stars"] == expected


def test_signal_labels(cfg):
    assert has_signal_label(["good first issue"], cfg.scoring) is True
    assert has_signal_label(["help wanted"], cfg.scoring) is True
    assert has_signal_label(["up-for-grabs"], cfg.scoring) is True  # alias -> easy
    assert has_signal_label(["bug"], cfg.scoring) is False
    assert has_signal_label(["performance"], cfg.scoring) is False
