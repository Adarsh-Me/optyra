"""Configuration loading: YAML files + environment variables for secrets.

The `.env` contract (report 02prd §6) is: GH_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
DATABASE_URL, AI_API_KEY, AI_MODEL, CONFIG_PATH, LOG_LEVEL, LOG_JSON, HEALTHCHECK_URL
(+ GITHUB_API_BASE / AI_API_BASE overrides; $PORT for PaaS healthz).

v2 schema: orgs.yaml holds the mission watch-set (orgs + pinned repos + blocked owners);
config.yaml holds intervals, the 10k star gate, recalibrated scoring, setup-filter
policy, notification budgets and the daily funnel report. Org list changes are pure
config — every weight/threshold is tunable without touching code.
"""

from __future__ import annotations

import hashlib
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


def _str_list(data: dict, key: str, where: str, default: list[str] | None = None) -> list[str]:
    value = data.get(key, default if default is not None else [])
    if not isinstance(value, list):
        raise ConfigError(f"config '{where}.{key}' must be a list")
    return [str(item).lower() for item in value]


# --------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class WatchConfig:
    orgs: tuple[str, ...]
    pinned_repos: tuple[str, ...]
    blocked_owners: frozenset[str]


@dataclass(frozen=True)
class SyncConfig:
    interval_hours: int
    min_stars: int
    per_page: int


@dataclass(frozen=True)
class PollConfig:
    interval_seconds: int  # v2: single tier — orgs + pinned scope all share this
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
    negative_labels: list[str]  # hard reject
    soft_labels: list[str]  # v2: "maintainer-parked" convention labels -> penalty, never drop


@dataclass(frozen=True)
class ScoringConfig:
    recency: tuple[tuple[int, int], ...]  # (max_age_seconds, points), ascending
    unassigned: int
    no_linked_pr: int
    labels: dict[str, int]
    label_points_cap: int
    label_aliases: dict[str, str]
    repo_pushed_days: int
    repo_pushed_window_days: int
    stars: tuple[tuple[int, int], ...]  # (min_stars, points), descending
    body_quality: int
    parked_penalty: int  # v2: subtracted when a soft label hits
    setup_minimal_bonus: int  # v2: +5 when AI says minimal setup


@dataclass(frozen=True)
class NotifyConfig:
    instant_threshold: int
    digest_threshold: int
    digest_interval_seconds: int
    max_messages_per_minute: int
    sender_concurrency: int
    per_owner_daily_cap: int  # v2: digest sends per repo-owner per UTC day
    digest_max_items: int  # v2: per-flush send cap; the rest are suppressed
    lane_signal_required: bool  # v2: digest lane needs a newcomer label OR worth=yes
    instant_lane_bypasses_gate: bool  # v2: score >= instant_threshold skips the lane gate


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
    setup_filter_default: str  # v2: "hard" (drop heavy) | "soft" (digest-only demote)
    org_setup_filter: dict[str, str]  # v2: per-org overrides, e.g. llvm: soft
    prior_min_samples: int  # v2: repo history needed before the prior gates anything
    daily_call_cap: int  # v2: hard Gemini cap per UTC day (free-tier protection)
    keyword_screen: bool  # v2: optional weak gate on AI-down days only (default off)
    setup_keywords: list[str]


@dataclass(frozen=True)
class FunnelConfig:
    report_hour_utc: int  # v2: daily funnel/self-report delivery hour
    report_enabled: bool


@dataclass(frozen=True)
class MaintenanceConfig:
    state_refresh_interval_seconds: int
    state_refresh_max_age_seconds: int
    state_refresh_batch: int
    prune_interval_seconds: int
    prune_after_days: int
    metrics_keep_days: int


@dataclass(frozen=True)
class OpsConfig:
    healthcheck_url: str
    healthcheck_timeout_seconds: int
    healthz_host: str
    healthz_port: int
    worker_lock_timeout_seconds: int


@dataclass(frozen=True)
class Secrets:
    gh_token: str
    telegram_bot_token: str
    telegram_chat_ids: tuple[int, ...]
    database_url: str
    github_api_base: str
    ai_api_base: str
    ai_api_key: str
    ai_model: str | None
    log_level: str
    log_json: bool
    healthcheck_url: str | None


@dataclass(frozen=True)
class AppConfig:
    watch: WatchConfig
    sync: SyncConfig
    poll: PollConfig
    filters: FiltersConfig
    scoring: ScoringConfig
    notify: NotifyConfig
    telegram: TelegramConfig
    ai: AiConfig
    funnel: FunnelConfig
    maintenance: MaintenanceConfig
    ops: OpsConfig
    ai_criteria: dict
    secrets: Secrets
    config_hash: str
    config_dir: Path = field(default=Path("config"))


# --------------------------------------------------------------------------- parsing


def _parse_watch(data: dict) -> WatchConfig:
    watch_raw = data.get("watch")
    if not isinstance(watch_raw, dict):
        raise ConfigError("orgs.yaml must contain a 'watch' mapping")
    orgs_raw = watch_raw.get("orgs")
    if not isinstance(orgs_raw, list) or not orgs_raw:
        raise ConfigError("orgs.yaml: 'watch.orgs' must be a non-empty list")
    logins: list[str] = []
    for i, item in enumerate(orgs_raw):
        if isinstance(item, dict):
            login = str(item.get("login", "")).strip().strip("/")
        else:
            login = str(item).strip().strip("/")
        if not login or "/" in login:
            raise ConfigError(f"orgs.yaml: watch.orgs[{i}] '{item}' is not an org login")
        if login.lower() in {x.lower() for x in logins}:
            raise ConfigError(f"orgs.yaml: duplicate org '{login}'")
        logins.append(login)
    pinned_raw = watch_raw.get("pinned_repos", []) or []
    if not isinstance(pinned_raw, list):
        raise ConfigError("orgs.yaml: 'watch.pinned_repos' must be a list")
    pinned: list[str] = []
    for i, item in enumerate(pinned_raw):
        name = str(item).strip().strip("/")
        parts = name.split("/")
        if len(parts) != 2 or not all(parts):
            raise ConfigError(f"orgs.yaml: watch.pinned_repos[{i}] '{item}' must be 'owner/name'")
        pinned.append(name)
    blocked = {
        str(item).strip().lower() for item in (watch_raw.get("blocked_owners", []) or []) if str(item).strip()
    }
    for name in pinned:
        if name.split("/")[0].lower() in blocked:
            raise ConfigError(f"orgs.yaml: pinned repo '{name}' has a blocked owner")
    return WatchConfig(orgs=tuple(logins), pinned_repos=tuple(pinned), blocked_owners=frozenset(blocked))


def _parse_scoring(data: dict) -> ScoringConfig:
    recency_raw = _get(data, "recency", {}, dict, "scoring")
    recency = sorted((_parse_duration(str(k)), int(v)) for k, v in recency_raw.items())
    labels = {str(k): int(v) for k, v in _get(data, "labels", {}, dict, "scoring").items()}
    aliases_raw = _get(data, "label_aliases", {}, dict, "scoring")
    aliases = {str(k).lower(): str(v) for k, v in aliases_raw.items()}
    stars_raw = _get(data, "stars", [], list, "scoring")
    stars = sorted(((int(entry["min"]), int(entry["points"])) for entry in stars_raw), reverse=True)
    return ScoringConfig(
        recency=tuple(recency),
        unassigned=int(_get(data, "unassigned", 20, int, "scoring")),
        no_linked_pr=int(_get(data, "no_linked_pr", 15, int, "scoring")),
        labels=labels,
        label_points_cap=int(_get(data, "label_points_cap", 20, int, "scoring")),
        label_aliases=aliases,
        repo_pushed_days=int(_get(data, "repo_pushed_days", 3, int, "scoring")),
        repo_pushed_window_days=int(_get(data, "repo_pushed_window_days", 30, int, "scoring")),
        stars=tuple(stars),
        body_quality=int(_get(data, "body_quality", 5, int, "scoring")),
        parked_penalty=int(_get(data, "parked_penalty", 8, int, "scoring")),
        setup_minimal_bonus=int(_get(data, "setup_minimal_bonus", 5, int, "scoring")),
    )


def _parse_duration(value: str) -> int:
    """'30m' / '2h' / '24h' / '7d' -> seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if value and value[-1].isdigit():
        return int(value)
    if value and value[-1].lower() in units and value[:-1].isdigit():
        return int(value[:-1]) * units[value[-1].lower()]
    raise ConfigError(f"invalid duration: {value!r} (use forms like 30m, 2h, 7d)")


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
    chat_ids = tuple(int(part) for part in chat_ids_raw.replace(" ", "").split(",") if part)
    return Secrets(
        gh_token=os.environ.get("GH_TOKEN", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_ids=chat_ids,
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://optyra:optyra@localhost:5432/optyra"
        ),
        github_api_base=os.environ.get("GITHUB_API_BASE", "https://api.github.com"),
        ai_api_base=os.environ.get("AI_API_BASE", "https://generativelanguage.googleapis.com"),
        ai_api_key=os.environ.get("AI_API_KEY", ""),
        ai_model=os.environ.get("AI_MODEL") or None,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        log_json=os.environ.get("LOG_JSON", "").lower() in ("1", "true", "yes"),
        healthcheck_url=os.environ.get("HEALTHCHECK_URL") or None,
    )


def _healthz_port(ops_raw: dict) -> int:
    """Health endpoint port: `$PORT` wins when set (Render/Heroku-style PaaS),
    otherwise the `ops.healthz_port` YAML value (default 8080, local dev)."""
    try:
        if int(os.environ.get("PORT") or 0) > 0:
            return int(os.environ["PORT"])
    except (TypeError, ValueError):
        pass
    return int(_get(ops_raw, "healthz_port", 8080, int, "ops"))


def load_config(config_dir: str | Path | None = None) -> AppConfig:
    """Load and validate the three YAML files; secrets come from the environment."""
    config_dir = Path(config_dir or os.environ.get("CONFIG_PATH", "config"))
    main_raw = _load_yaml(config_dir / "config.yaml")
    orgs_raw = _load_yaml(config_dir / "orgs.yaml")
    criteria_raw = _load_yaml(config_dir / "ai_criteria.yaml")

    digest = hashlib.sha256()
    for name in ("config.yaml", "orgs.yaml", "ai_criteria.yaml"):
        digest.update((config_dir / name).read_bytes())
    config_hash = digest.hexdigest()[:8]

    sync_raw = _section(main_raw, "sync")
    sync = SyncConfig(
        interval_hours=int(_get(sync_raw, "interval_hours", 6, int, "sync")),
        min_stars=int(_get(sync_raw, "min_stars", 10000, int, "sync")),
        per_page=int(_get(sync_raw, "per_page", 100, int, "sync")),
    )

    poll_raw = _section(main_raw, "poll")
    poll = PollConfig(
        interval_seconds=int(_get(poll_raw, "interval_seconds", 180, int, "poll")),
        overlap_seconds=int(_get(poll_raw, "overlap_seconds", 120, int, "poll")),
        max_backfill_hours=int(_get(poll_raw, "max_backfill_hours", 24, int, "poll")),
        max_catchup_hours=int(_get(poll_raw, "max_catchup_hours", 72, int, "poll")),
        catchup_window_seconds=int(_get(poll_raw, "catchup_window_minutes", 60, int, "poll")) * 60,
        page_size=int(_get(poll_raw, "page_size", 100, int, "poll")),
        breaker_failures=int(_get(poll_raw, "breaker_failures", 5, int, "poll")),
        breaker_cooldown_seconds=int(_get(poll_raw, "breaker_cooldown_seconds", 900, int, "poll")),
        concurrency=int(_get(poll_raw, "concurrency", 4, int, "poll")),
    )

    filters_raw = _section(main_raw, "filters")
    filters = FiltersConfig(
        max_age_hours=int(_get(filters_raw, "max_age_hours", 72, int, "filters")),
        min_body_chars=int(_get(filters_raw, "min_body_chars", 50, int, "filters")),
        bot_author_suffixes=[
            str(s) for s in _get(filters_raw, "bot_author_suffixes", ["bot"], list, "filters")
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
        negative_labels=_str_list(
            filters_raw,
            "negative_labels",
            "filters",
            ["question", "support", "invalid", "duplicate", "wontfix", "security"],
        ),
        soft_labels=_str_list(
            filters_raw,
            "soft_labels",
            "filters",
            [
                "needs triage",
                "needs-triage",
                "awaiting triage",
                "awaiting-triage",
                "needs more info",
                "needs-more-info",
                "needs-reproduction",
                "waiting on author",
                "waiting-on-author",
                "discussion",
                "rfc",
            ],
        ),
    )

    scoring = _parse_scoring(_section(main_raw, "scoring"))

    notify_raw = _section(main_raw, "notify")
    notify = NotifyConfig(
        instant_threshold=int(_get(notify_raw, "instant_threshold", 85, int, "notify")),
        digest_threshold=int(_get(notify_raw, "digest_threshold", 70, int, "notify")),
        digest_interval_seconds=int(_get(notify_raw, "digest_interval_minutes", 20, int, "notify")) * 60,
        max_messages_per_minute=int(_get(notify_raw, "max_messages_per_minute", 15, int, "notify")),
        sender_concurrency=int(_get(notify_raw, "sender_concurrency", 2, int, "notify")),
        per_owner_daily_cap=int(_get(notify_raw, "per_owner_daily_cap", 5, int, "notify")),
        digest_max_items=int(_get(notify_raw, "digest_max_items", 20, int, "notify")),
        lane_signal_required=bool(_get(notify_raw, "lane_signal_required", True, bool, "notify")),
        instant_lane_bypasses_gate=bool(_get(notify_raw, "instant_lane_bypasses_gate", True, bool, "notify")),
    )

    telegram_raw = _section(main_raw, "telegram")
    telegram = TelegramConfig(
        api_base=str(_get(telegram_raw, "api_base", "https://api.telegram.org", str, "telegram")),
        parse_mode=str(_get(telegram_raw, "parse_mode", "HTML", str, "telegram")),
    )

    ai_raw = _section(main_raw, "ai")
    org_filter_raw = _get(ai_raw, "org_setup_filter", {}, dict, "ai")
    for org, mode in org_filter_raw.items():
        if str(mode) not in ("hard", "soft"):
            raise ConfigError(f"ai.org_setup_filter.{org} must be 'hard' or 'soft', got {mode!r}")
    setup_default = str(_get(ai_raw, "setup_filter_default", "hard", str, "ai"))
    if setup_default not in ("hard", "soft"):
        raise ConfigError("ai.setup_filter_default must be 'hard' or 'soft'")
    ai = AiConfig(
        enabled=bool(_get(ai_raw, "enabled", True, bool, "ai")),
        timeout_seconds=int(_get(ai_raw, "timeout_seconds", 20, int, "ai")),
        max_retries=int(_get(ai_raw, "max_retries", 2, int, "ai")),
        max_body_chars=int(_get(ai_raw, "max_body_chars", 3000, int, "ai")),
        summary_max_chars=int(_get(ai_raw, "summary_max_chars", 220, int, "ai")),
        model=str(_get(ai_raw, "model", "gemini-2.0-flash", str, "ai")),
        setup_filter_default=setup_default,
        org_setup_filter={str(k).lower(): str(v) for k, v in org_filter_raw.items()},
        prior_min_samples=int(_get(ai_raw, "prior_min_samples", 5, int, "ai")),
        daily_call_cap=int(_get(ai_raw, "daily_call_cap", 300, int, "ai")),
        keyword_screen=bool(_get(ai_raw, "keyword_screen", False, bool, "ai")),
        setup_keywords=_str_list(
            ai_raw,
            "setup_keywords",
            "ai",
            ["xcode", "android ndk", "emulator", "cuda", "gpu", "proprietary", "specialized hardware"],
        ),
    )

    funnel_raw = _section(main_raw, "funnel")
    funnel = FunnelConfig(
        report_hour_utc=int(_get(funnel_raw, "report_hour_utc", 21, int, "funnel")),
        report_enabled=bool(_get(funnel_raw, "report_enabled", True, bool, "funnel")),
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
        state_refresh_batch=int(_get(maint_raw, "state_refresh_batch", 50, int, "maintenance")),
        prune_interval_seconds=int(_get(maint_raw, "prune_interval_hours", 24, int, "maintenance")) * 3600,
        prune_after_days=int(_get(maint_raw, "prune_after_days", 90, int, "maintenance")),
        metrics_keep_days=int(_get(maint_raw, "metrics_keep_days", 30, int, "maintenance")),
    )

    ops_raw = _section(main_raw, "ops")
    ops = OpsConfig(
        healthcheck_url=str(_get(ops_raw, "healthcheck_url", "", str, "ops")),
        healthcheck_timeout_seconds=int(_get(ops_raw, "healthcheck_timeout_seconds", 10, int, "ops")),
        healthz_host=str(_get(ops_raw, "healthz_host", "0.0.0.0", str, "ops")),
        healthz_port=_healthz_port(ops_raw),
        worker_lock_timeout_seconds=int(_get(ops_raw, "worker_lock_timeout_seconds", 180, int, "ops")),
    )

    secrets = _secrets_from_env()
    if not secrets.gh_token:
        raise ConfigError("GH_TOKEN environment variable is required (fine-grained PAT, public read-only)")

    return AppConfig(
        watch=_parse_watch(orgs_raw),
        sync=sync,
        poll=poll,
        filters=filters,
        scoring=scoring,
        notify=notify,
        telegram=telegram,
        ai=ai,
        funnel=funnel,
        maintenance=maintenance,
        ops=ops,
        ai_criteria=criteria_raw,
        secrets=secrets,
        config_hash=config_hash,
        config_dir=config_dir,
    )
