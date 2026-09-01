"""SQLAlchemy 2.0 models (report 01prd §6 schema, plus columns required by the feature set).

JSON columns use JSONB on PostgreSQL and plain JSON on SQLite (tests).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

JSONType = JSONB().with_variant(JSON(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Org(Base):
    __tablename__ = "orgs"

    login: Mapped[str] = mapped_column(String(100), primary_key=True)
    tier: Mapped[int] = mapped_column(SmallInteger, default=2, nullable=False)
    gsoc_years: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    # Cached org-level GSoC relevance score (computed nightly, stamped onto new issues).
    gsoc_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gsoc_components: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    gsoc_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Repo(Base):
    __tablename__ = "repos"

    github_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_login: Mapped[str] = mapped_column(String(100), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    stars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    language: Mapped[str | None] = mapped_column(String(50))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_repos_org_monitored", "org_login", "monitored"),)


class Issue(Base):
    __tablename__ = "issues"
    # Composite PK (repo_full_name, number) IS the dedupe key (report §6).

    repo_full_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    author: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    labels: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    assignees: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    # Deep-check results (timeline cross-references + fresh issue fetch).
    linked_pr: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    linked_open_pr: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_comment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Scores (rule score 0-100 at detection; gsoc score cached from org row).
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gsoc_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    filtered_reason: Mapped[str | None] = mapped_column(String(100))
    # AI enrichment (report 02prd §4): one call per issue, cached.
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_worth_attempting: Mapped[bool | None] = mapped_column(Boolean)
    ai_difficulty: Mapped[str | None] = mapped_column(String(20))
    ai_reason_codes: Mapped[list | None] = mapped_column(JSONType)
    ai_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ai_model: Mapped[str | None] = mapped_column(String(100))
    # Bookkeeping.
    raw: Mapped[dict | None] = mapped_column(JSONType)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_issues_score", "score"),
        Index("ix_issues_first_seen", "first_seen_at"),
        Index("ix_issues_state", "state"),
    )

    @property
    def issue_key(self) -> str:
        return f"{self.repo_full_name}#{self.number}"

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.repo_full_name}/issues/{self.number}"


class Notification(Base):
    __tablename__ = "notifications"
    # PK (issue_key, channel) enforces exactly-once notification per channel (report §16).

    issue_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    channel: Mapped[str] = mapped_column(String(50), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_notifications_sent", "sent_at"),)


class PollState(Base):
    __tablename__ = "poll_state"
    # The reliability backbone (report §16): per-org watermark + health + circuit breaker.

    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_ok: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    breaker_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetaInfo(Base):
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


SCHEMA_VERSION = "1"
