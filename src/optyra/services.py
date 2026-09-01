"""Shared job dependencies, passed to every job (one wiring point, no circular imports)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from optyra.ai.enricher import IssueEnricher
from optyra.config import AppConfig
from optyra.github.client import GitHubClient
from optyra.health import HealthState
from optyra.notify.telegram import TelegramNotifier


@dataclass
class Services:
    cfg: AppConfig
    session_factory: async_sessionmaker[AsyncSession]
    gh: GitHubClient
    health: HealthState
    tg: TelegramNotifier | None = None
    enricher: IssueEnricher | None = None
