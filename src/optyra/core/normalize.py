"""Normalize GitHub API payloads into typed structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from optyra.github.client import _parse_gh_time


@dataclass
class ParsedIssue:
    repo_full_name: str
    number: int
    title: str
    state: str
    author: str | None
    created_at: datetime
    labels: list[str]
    assignees: list[str]
    body: str
    html_url: str
    raw: dict = field(default_factory=dict)

    @property
    def issue_key(self) -> str:
        return f"{self.repo_full_name}#{self.number}"


def _repo_full_name(item: dict) -> str | None:
    url = item.get("repository_url") or ""
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return None


def _label_names(item: dict) -> list[str]:
    names = []
    for label in item.get("labels") or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = str(label)
        if name:
            names.append(str(name).lower())
    return names


def _assignee_logins(item: dict) -> list[str]:
    logins = []
    for person in item.get("assignees") or []:
        if isinstance(person, dict) and person.get("login"):
            logins.append(str(person["login"]))
    return logins


def parse_search_item(item: dict, *, body_max_chars: int | None = None) -> ParsedIssue | None:
    """Parse a /search/issues item (also works for REST issue payloads).

    Returns None for pull requests (is:issue should exclude them, but the search index
    occasionally leaks PRs — we never want them) or items missing key fields.
    """
    if "pull_request" in item:
        return None
    full_name = _repo_full_name(item)
    number = item.get("number")
    created_at = _parse_gh_time(item.get("created_at"))
    if not full_name or not isinstance(number, int) or created_at is None:
        return None
    body = item.get("body") or ""
    raw = dict(item)
    if body_max_chars is not None and isinstance(raw.get("body"), str):
        raw["body"] = raw["body"][:body_max_chars]
    author = None
    user = item.get("user")
    if isinstance(user, dict) and user.get("login"):
        author = str(user["login"])
    return ParsedIssue(
        repo_full_name=full_name,
        number=number,
        title=str(item.get("title") or ""),
        state=str(item.get("state") or "open"),
        author=author,
        created_at=created_at,
        labels=_label_names(item),
        assignees=_assignee_logins(item),
        body=body if isinstance(body, str) else "",
        html_url=str(item.get("html_url") or f"https://github.com/{full_name}/issues/{number}"),
        raw=raw,
    )


@dataclass
class ParsedRepo:
    github_id: int
    org_login: str
    full_name: str
    stars: int
    language: str | None
    archived: bool
    pushed_at: datetime | None


def parse_repo_item(item: dict) -> ParsedRepo | None:
    repo_id = item.get("id")
    full_name = item.get("full_name")
    if not isinstance(repo_id, int) or not full_name:
        return None
    owner = (item.get("owner") or {}).get("login", "")
    return ParsedRepo(
        github_id=repo_id,
        org_login=str(owner),
        full_name=str(full_name),
        stars=int(item.get("stargazers_count") or 0),
        language=item.get("language") or None,
        archived=bool(item.get("archived") or False),
        pushed_at=_parse_gh_time(item.get("pushed_at")),
    )
