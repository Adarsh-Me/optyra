"""Hard filters (report 01prd §10/§11): applied before scoring; any hit => never notified."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from optyra.config import FiltersConfig
from optyra.core.normalize import ParsedIssue


@dataclass
class FilterResult:
    ok: bool
    reason: str | None = None


def _is_bot(author: str | None, cfg: FiltersConfig) -> bool:
    if not author:
        return False
    if author.lower() in {b.lower() for b in cfg.bot_author_logins}:
        return True
    return any(author.lower().endswith(suffix.lower()) for suffix in cfg.bot_author_suffixes)


def _body_quality_signal(body: str) -> bool:
    """>=200 chars AND contains a code block, error text, or numbered steps (report §11)."""
    if len(body) < 200:
        return False
    markers = ("```", "Traceback (most recent call last)", "Error:", "\n1.", "\n1)")
    return any(marker in body for marker in markers)


def canonical_labels(labels: list[str], aliases: dict[str, str]) -> list[str]:
    """Map org-specific label conventions onto canonical scoring names."""
    out = []
    for label in labels:
        mapped = aliases.get(label.lower(), label.lower())
        out.append(mapped)
    return out


def hard_filter(
    issue: ParsedIssue,
    cfg: FiltersConfig,
    *,
    now: datetime | None = None,
    repo_monitored: bool = True,
) -> FilterResult:
    now = now or datetime.now(UTC)
    if issue.state != "open":
        return FilterResult(False, "closed")
    if issue.assignees:
        return FilterResult(False, "assigned")
    label_set = {label.lower() for label in issue.labels}
    negative = {n.lower() for n in cfg.negative_labels}
    hit = label_set & negative
    if hit:
        return FilterResult(False, f"negative-label:{sorted(hit)[0]}")
    if not repo_monitored:
        return FilterResult(False, "repo-not-whitelisted")
    age_hours = (now - issue.created_at).total_seconds() / 3600
    if age_hours > cfg.max_age_hours:
        return FilterResult(False, "too-old")
    if _is_bot(issue.author, cfg):
        return FilterResult(False, "bot-author")
    if len(issue.body) < cfg.min_body_chars:
        return FilterResult(False, "body-too-short")
    return FilterResult(True, None)


def body_quality_points(body: str) -> bool:
    return _body_quality_signal(body)
