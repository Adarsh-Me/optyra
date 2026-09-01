"""Payload normalization tests."""

from __future__ import annotations

from conftest import make_issue_item, make_repo_item
from optyra.core.normalize import parse_repo_item, parse_search_item


def test_parse_issue_item():
    parsed = parse_search_item(make_issue_item(labels=("Good First Issue", "Bug")))
    assert parsed is not None
    assert parsed.repo_full_name == "apache/kafka"
    assert parsed.number == 1
    assert parsed.labels == ["good first issue", "bug"]
    assert parsed.author == "alice"
    assert parsed.assignees == []
    assert parsed.html_url == "https://github.com/apache/kafka/issues/1"
    assert "NullPointerException" in parsed.title
    assert parsed.raw["body"].startswith("When running")


def test_parse_issue_item_raw_body_truncated():
    item = make_issue_item()
    item["body"] = "x" * 5000
    parsed = parse_search_item(item, body_max_chars=2000)
    assert len(parsed.raw["body"]) == 2000
    assert len(parsed.body) == 5000


def test_pull_requests_rejected():
    item = make_issue_item()
    item["pull_request"] = {"html_url": "https://github.com/apache/kafka/pull/1"}
    assert parse_search_item(item) is None


def test_malformed_items_rejected():
    assert parse_search_item({}) is None
    item = make_issue_item()
    item["repository_url"] = "https://api.github.com/repos/"
    assert parse_search_item(item) is None


def test_assignees_and_labels_variants():
    item = make_issue_item(assignees=("bob",), labels=("help wanted",))
    parsed = parse_search_item(item)
    assert parsed.assignees == ["bob"]


def test_parse_repo_item():
    parsed = parse_repo_item(make_repo_item("apache/kafka", stars=28000))
    assert parsed is not None
    assert parsed.github_id > 0
    assert parsed.org_login == "apache"
    assert parsed.full_name == "apache/kafka"
    assert parsed.stars == 28000
    assert parsed.archived is False
    assert parsed.pushed_at is not None
