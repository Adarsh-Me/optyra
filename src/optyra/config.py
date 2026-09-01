"""Configuration loading: YAML files + environment variables for secrets.

The `.env` contract (report 02prd §6) is: GH_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
DATABASE_URL, AI_API_KEY, AI_MODEL, CONFIG_PATH, LOG_LEVEL, LOG_JSON, HEALTHCHECK_URL.
All knobs from the report (§7-§11) live in config/config.yaml; orgs live in config/orgs.yaml;
LLM criteria in config/ai_criteria.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- helpers


def _section(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"config section '{key}' must be a mapping")
    return value


def _get(data: dict, key: str, default, types: type | tuple, where: str):
    value = data.get(key, default)
    if isinstance(value, types):
        return value
    raise ConfigError(f"config '{where}.{key}' must be {types}, got {type(value).__name__}")


def _parse_duration(value: str) -> int:
    """'30m' / '2h' / '24h' / '7d' -> seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if value and value[-1].isdigit():
        return int(value)
    if value and value[-1].lower() in units and value[:-1].isdigit():
        return int(value[:-1]) * units[value[-1].lower()]
    raise ConfigError(f"invalid duration: {value!r} (use forms like 30m, 2h, 7d)")


# --------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class SyncConfig:
    interval_hours: int
    min_stars: int
    per_page: int


@dataclass(frozen=True)
class PollConfig:
    tier1_interval_seconds: int
    tier2_interval_seconds: int
    overlap_seconds: int
    max_backfill_hours: int
    max_catchup_hours: int
    catchup_window_seconds: int
    page_size: int
    breaker_failures: int
    breaker_cooldown_seconds: int
    concurrency: int


@dataclass(frozen=True)
class FiltersConfig:
    max_age_hours: int
    min_body_chars: int
    bot_author_suffixes: list[str]
    bot_author_logins: list[str]
    negative_labels: list[str]


@dataclass(frozen=True)
class ScoringConfig:
    recency: tuple[tuple[int, int], ...]  # (max_age_seconds, points), ascending
    unassigned: int
    no_linked_pr: int
    labels: dict[str, int]
    label_aliases: dict[str, str]
    repo_pushed_days: int
    repo_pushed_window_days: int
    stars: tuple[tuple[int, int], ...]  # (min_stars, points), descending
    body_quality: int
    gsoc_years: tuple[tuple[int, int], ...]  # (min_participations, points), descending
    gsoc_mega_repo: int
    gsoc_newcomer_ratio: int
    gsoc_triage: int


@dataclass(frozen=True)
class NotifyConfig:
    instant_threshold: int
    digest_threshold: int
    digest_interval_seconds: int
    digest_max_age_seconds: int
    max_messages_per_minute: int
    sender_concurrency: int


@dataclass(frozen=True)
class TelegramConfig:
    api_base: str
    parse_mode: str


@dataclass(frozen=True)
class AiConfig:
    enabled: bool
    timeout_seconds: int
    max_retries: int
    max_body_chars: int
    summary_max_chars: int
    model: str


@dataclass(frozen=True)
class MaintenanceConfig:
    state_refresh_interval_seconds: int
    state_refresh_max_age_seconds: int
    state_refresh_batch: int
    prune_interval_seconds: int
    prune_after_days: int


@dataclass(frozen=True)
class OpsConfig:
    healthcheck_url: str
    healthcheck_timeout_seconds: int
    healthz_host: str
    healthz_port: int


@dataclass(frozen=True)
class OrgEntry:
    login: str
    tier: int
    gsoc_years: list[int]


@dataclass(frozen=True)
class Secrets:
    gh_token: str
    telegram_bot_token: str
    telegram_chat_ids: tuple[int, ...]
    database_url: str
    ai_api_key: str
    ai_model: str | None
    log_level: str
    log_json: bool
    healthcheck_url: str | None


@dataclass(frozen=True)
class AppConfig:
    sync: SyncConfig
    poll: PollConfig
    filters: FiltersConfig
    scoring: ScoringConfig
    notify: NotifyConfig
    telegram: TelegramConfig
    ai: AiConfig
    maintenance: MaintenanceConfig
    ops: OpsConfig
    orgs: tuple[OrgEntry, ...]
    ai_criteria: dict
    secrets: Secrets
    config_dir: Path = field(default=Path("config"))


# --------------------------------------------------------------------------- parsing


def _parse_scoring(data: dict) -> ScoringConfig:
    recency_raw = _get(data, "recency", {}, dict, "scoring")
    recency = sorted(
        (_parse_duration(str(k)), int(v)) for k, v in recency_raw.items()
    )
    labels_raw = _get(data, "labels", {}, dict, "scoring")
    labels = {str(k): int(v) for k, v in labels_raw.items()}
    aliases_raw = _get(data, "label_aliases", {}, dict, "scoring")
    aliases = {str(k).lower(): str(v) for k, v in aliases_raw.items()}
    stars_raw = _get(data, "stars", [], list, "scoring")
    stars = sorted(
        ((int(entry["min"]), int(entry["points"])) for entry in stars_raw), reverse=True
    )
    gsoc_raw = _get(data, "gsoc", {}, dict, "scoring")
    years_raw = _get(gsoc_raw, "years", {}, dict, "scoring.gsoc")
    years = sorted(((int(k), int(v)) for k, v in years_raw.items()), reverse=True)
    return ScoringConfig(
        recency=tuple(recency),
        unassigned=int(_get(data, "unassigned", 20, int, "scoring")),
        no_linked_pr=int(_get(data, "no_linked_pr", 15, int, "scoring")),
        labels=labels,
        label_aliases=aliases,
        repo_pushed_days=int(_get(data, "repo_pushed_days", 10, int, "scoring")),
        repo_pushed_window_days=int(_get(data, "repo_pushed_window_days", 30, int, "scoring")),
        stars=tuple(stars),
        body_quality=int(_get(data, "body_quality", 5, int, "scoring")),
        gsoc_years=tuple(years),
        gsoc_mega_repo=int(_get(gsoc_raw, "mega_repo", 20, int, "scoring.gsoc")),
        gsoc_newcomer_ratio=int(_get(gsoc_raw, "newcomer_ratio", 20, int, "scoring.gsoc")),
        gsoc_triage=int(_get(gsoc_raw, "triage", 20, int, "scoring.gsoc")),
    )


def _parse_orgs(data: dict) -> tuple[OrgEntry, ...]:
    orgs_raw = data.get("orgs")
    if not isinstance(orgs_raw, list) or not orgs_raw:
        raise ConfigError("orgs.yaml must contain a non-empty 'orgs' list")
    entries = []
    seen = set()
    for i, item in enumerate(orgs_raw):
        if not isinstance(item, dict) or "login" not in item:
            raise ConfigError(f"orgs.yaml entry #{i} must be a mapping with 'login'")
        login = str(item["login"]).strip().strip("/")
        if not login or "/" in login:
            raise ConfigError(f"orgs.yaml entry #{i}: '{login}' is not an org login")
        if login.lower() in seen:
            raise ConfigError(f"orgs.yaml: duplicate org '{login}'")
        seen.add(login.lower())
        tier = int(_get(item, "tier", 2, int, f"orgs.{login}"))
        if tier not in (1, 2):
            raise ConfigError(f"orgs.{login}.tier must be 1 or 2, got {tier}")
        years = [int(y) for y in _get(item, "gsoc_years", [], list, f"orgs.{login}")]
        entries.append(OrgEntry(login=login, tier=tier, gsoc_years=years))
    return tuple(entries)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def _secrets_from_env() -> Secrets:
    chat_ids_raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    chat_ids = tuple(
        int(part) for part in chat_ids_raw.replace(" ", "").split(",") if part
    )
    return Secrets(
        gh_token=os.environ.get("GH_TOKEN", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_ids=chat_ids,
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://optyra:optyra@localhost:5432/optyra"
        ),
        ai_api_key=os.environ.get("AI_API_KEY", ""),
        ai_model=os.environ.get("AI_MODEL") or None,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        log_json=os.environ.get("LOG_JSON", "").lower() in ("1", "true", "yes"),
        healthcheck_url=os.environ.get("HEALTHCHECK_URL") or None,
    )


def load_config(config_dir: str | Path | None = None) -> AppConfig:
    """Load and validate the three YAML files; secrets come from the environment."""
    config_dir = Path(config_dir or os.environ.get("CONFIG_PATH", "config"))
    main_raw = _load_yaml(config_dir / "config.yaml")
    orgs_raw = _load_yaml(config_dir / "orgs.yaml")
    criteria_raw = _load_yaml(config_dir / "ai_criteria.yaml")

    sync_raw = _section(main_raw, "sync")
    sync = SyncConfig(
        interval_hours=int(_get(sync_raw, "interval_hours", 24, int, "sync")),
        min_stars=int(_get(sync_raw, "min_stars", 2000, int, "sync")),
        per_page=int(_get(sync_raw, "per_page", 100, int, "sync")),
    )

    poll_raw = _section(main_raw, "poll")
    poll = PollConfig(
        tier1_interval_seconds=int(
            _get(poll_raw, "tier1_interval_seconds", 180, int, "poll")
        ),
        tier2_interval_seconds=int(
            _get(poll_raw, "tier2_interval_seconds", 720, int, "poll")
        ),
        overlap_seconds=int(_get(poll_raw, "overlap_seconds", 120, int, "poll")),
        max_backfill_hours=int(_get(poll_raw, "max_backfill_hours", 24, int, "poll")),
        max_catchup_hours=int(_get(poll_raw, "max_catchup_hours", 72, int, "poll")),
        catchup_window_seconds=int(
            _get(poll_raw, "catchup_window_minutes", 60, int, "poll")
        )
        * 60,
        page_size=int(_get(poll_raw, "page_size", 100, int, "poll")),
        breaker_failures=int(_get(poll_raw, "breaker_failures", 5, int, "poll")),
        breaker_cooldown_seconds=int(
            _get(poll_raw, "breaker_cooldown_seconds", 900, int, "poll")
        ),
        concurrency=int(_get(poll_raw, "concurrency", 4, int, "poll")),
    )

    filters_raw = _section(main_raw, "filters")
    filters = FiltersConfig(
        max_age_hours=int(_get(filters_raw, "max_age_hours", 72, int, "filters")),
        min_body_chars=int(_get(filters_raw, "min_body_chars", 50, int, "filters")),
        bot_author_suffixes=[
            str(s)
            for s in _get(
                filters_raw, "bot_author_suffixes", ["bot"], list, "filters"
            )
        ],
        bot_author_logins=[
            str(s)
            for s in _get(
                filters_raw,
                "bot_author_logins",
                ["github-actions", "dependabot", "renovate-bot"],
                list,
                "filters",
            )
        ],
        negative_labels=[
            str(l).lower()
            for l in _get(
                filters_raw,
                "negative_labels",
                ["question", "support", "invalid", "duplicate", "wontfix", "security"],
                list,
                "filters",
            )
        ],
    )

    scoring = _parse_scoring(_section(main_raw, "scoring"))

    notify_raw = _section(main_raw, "notify")
    notify = NotifyConfig(
        instant_threshold=int(_get(notify_raw, "instant_threshold", 85, int, "notify")),
        digest_threshold=int(_get(notify_raw, "digest_threshold", 70, int, "notify")),
        digest_interval_seconds=int(
            _get(notify_raw, "digest_interval_minutes", 20, int, "notify")
        )
        * 60,
        digest_max_age_seconds=int(
            _get(notify_raw, "digest_max_age_hours", 6, int, "notify")
        )
        * 3600,
        max_messages_per_minute=int(
            _get(notify_raw, "max_messages_per_minute", 15, int, "notify")
        ),
        sender_concurrency=int(
            _get(notify_raw, "sender_concurrency", 2, int, "notify")
        ),
    )

    telegram_raw = _section(main_raw, "telegram")
    telegram = TelegramConfig(
        api_base=str(_get(telegram_raw, "api_base", "https://api.telegram.org", str, "telegram")),
        parse_mode=str(_get(telegram_raw, "parse_mode", "HTML", str, "telegram")),
    )

    ai_raw = _section(main_raw, "ai")
    ai = AiConfig(
        enabled=bool(_get(ai_raw, "enabled", True, bool, "ai")),
        timeout_seconds=int(_get(ai_raw, "timeout_seconds", 20, int, "ai")),
        max_retries=int(_get(ai_raw, "max_retries", 2, int, "ai")),
        max_body_chars=int(_get(ai_raw, "max_body_chars", 3000, int, "ai")),
        summary_max_chars=int(_get(ai_raw, "summary_max_chars", 220, int, "ai")),
        model=str(_get(ai_raw, "model", "gemini-2.0-flash", str, "ai")),
    )

    maint_raw = _section(main_raw, "maintenance")
    maintenance = MaintenanceConfig(
        state_refresh_interval_seconds=int(
            _get(maint_raw, "state_refresh_interval_minutes", 60, int, "maintenance")
        )
        * 60,
        state_refresh_max_age_seconds=int(
            _get(maint_raw, "state_refresh_max_age_hours", 48, int, "maintenance")
        )
        * 3600,
        state_refresh_batch=int(
            _get(maint_raw, "state_refresh_batch", 50, int, "maintenance")
        ),
        prune_interval_seconds=int(
            _get(maint_raw, "prune_interval_hours", 24, int, "maintenance")
        )
        * 3600,
        prune_after_days=int(_get(maint_raw, "prune_after_days", 90, int, "maintenance")),
    )

    ops_raw = _section(main_raw, "ops")
    ops = OpsConfig(
        healthcheck_url=str(_get(ops_raw, "healthcheck_url", "", str, "ops")),
        healthcheck_timeout_seconds=int(
            _get(ops_raw, "healthcheck_timeout_seconds", 10, int, "ops")
        ),
        healthz_host=str(_get(ops_raw, "healthz_host", "0.0.0.0", str, "ops")),
        healthz_port=int(_get(ops_raw, "healthz_port", 8080, int, "ops")),
    )

    secrets = _secrets_from_env()
    if not secrets.gh_token:
        raise ConfigError("GH_TOKEN environment variable is required (fine-grained PAT, public read-only)")

    return AppConfig(
        sync=sync,
        poll=poll,
        filters=filters,
        scoring=scoring,
        notify=notify,
        telegram=telegram,
        ai=ai,
        maintenance=maintenance,
        ops=ops,
        orgs=_parse_orgs(orgs_raw),
        ai_criteria=criteria_raw,
        secrets=secrets,
        config_dir=config_dir,
    )
