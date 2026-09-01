"""Hard filter tests (report §10/§11)."""

from __future__ import annotations

from conftest import make_issue_item, utcnow
from optyra.core.filters import body_quality_points, hard_filter
from optyra.core.normalize import parse_search_item


def _filter(cfg, item, **kw):
    parsed = parse_search_item(item)
    assert parsed is not None
    return hard_filter(parsed, cfg.filters, now=utcnow(), **kw)


def test_passes(cfg):
    result = _filter(cfg, make_issue_item())
    assert result.ok and result.reason is None


def test_closed(cfg):
    item = make_issue_item()
    item["state"] = "closed"
    assert _filter(cfg, item).reason == "closed"


def test_assigned(cfg):
    assert _filter(cfg, make_issue_item(assignees=("bob",))).reason == "assigned"


def test_negative_labels(cfg):
    for label in ("question", "support", "invalid", "duplicate", "wontfix", "security"):
        item = make_issue_item(labels=(label,))
        assert _filter(cfg, item).reason == f"negative-label:{label}", label


def test_too_old(cfg):
    assert _filter(cfg, make_issue_item(created_min_ago=60 * 24 * 5)).reason == "too-old"


def test_bot_author(cfg):
    assert _filter(cfg, make_issue_item(author="dependabot")).reason == "bot-author"
    assert _filter(cfg, make_issue_item(author="someuser-bot")).reason == "bot-author"


def test_short_body(cfg):
    assert _filter(cfg, make_issue_item(body="help pls")).reason == "body-too-short"


def test_repo_not_whitelisted(cfg):
    result = _filter(cfg, make_issue_item(), repo_monitored=False)
    assert result.reason == "repo-not-whitelisted"


def test_body_quality_signal(cfg):
    good = make_issue_item().get("body")
    assert body_quality_points(good) is True
    assert body_quality_points("short") is False
    long_flat = "This issue describes a problem. " * 10
    assert body_quality_points(long_flat) is False
    assert body_quality_points(long_flat + "\n1. first step\n2. second step") is True
    assert body_quality_points(long_flat + "\nError: connection refused") is True
