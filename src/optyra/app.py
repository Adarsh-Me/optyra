"""Application wiring: config -> engine -> clients -> jobs, run under a TaskGroup.

Job isolation: each job catches its own exceptions (only cancellation propagates), so one
failing job can never kill the worker. `main` stays deployable via plain `python -m optyra`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC

from optyra.ai.enricher import IssueEnricher
from optyra.config import AppConfig, load_config
from optyra.db.dal import DAL, utcnow_aware
from optyra.db.session import WorkerLock, create_engine, create_session_factory, ensure_schema
from optyra.github.client import GitHubClient
from optyra.github.token_bucket import TokenBucket
from optyra.health import HealthcheckPinger, HealthState, start_health_server
from optyra.jobs.issue_poll import IssuePoller
from optyra.jobs.maintenance import DigestFlushJob, MaintenanceJob
from optyra.jobs.repo_sync import RepoSyncJob
from optyra.jobs.state_refresh import StateRefreshJob
from optyra.logging_setup import SCRUBBER, setup_logging
from optyra.notify.telegram import TelegramNotifier
from optyra.services import Services

logger = logging.getLogger(__name__)


class App:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.engine = create_engine(cfg.secrets.database_url)
        self.session_factory = create_session_factory(self.engine)
        self.lock = WorkerLock(self.engine)
        self.bucket = TokenBucket(20)  # report §7: never exceed ~20 search req/min
        self.gh = GitHubClient(
            cfg.secrets.gh_token,
            bucket=self.bucket,
            rest_concurrency=5,
            base_url=cfg.secrets.github_api_base,
        )
        self.tg: TelegramNotifier | None = None
        if cfg.secrets.telegram_bot_token and cfg.secrets.telegram_chat_ids:
            self.tg = TelegramNotifier(
                cfg.secrets.telegram_bot_token,
                list(cfg.secrets.telegram_chat_ids),
                api_base=cfg.telegram.api_base,
                parse_mode=cfg.telegram.parse_mode,
            )
        else:
            logger.warning("Telegram disabled: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        self.enricher: IssueEnricher | None = None
        if cfg.ai.enabled and cfg.secrets.ai_api_key:
            self.enricher = IssueEnricher(
                api_key=cfg.secrets.ai_api_key,
                model=cfg.secrets.ai_model or cfg.ai.model,
                criteria=cfg.ai_criteria,
                timeout_seconds=cfg.ai.timeout_seconds,
                max_retries=cfg.ai.max_retries,
                max_body_chars=cfg.ai.max_body_chars,
                summary_max_chars=cfg.ai.summary_max_chars,
                base_url=cfg.secrets.ai_api_base,
            )
        else:
            logger.warning("AI enrichment disabled (AI_API_KEY missing or ai.enabled=false)")
        self.health = HealthState()
        self.pinger = HealthcheckPinger(
            cfg.secrets.healthcheck_url or cfg.ops.healthcheck_url or None,
            timeout=cfg.ops.healthcheck_timeout_seconds,
        )
        self.services = Services(
            cfg=cfg,
            session_factory=self.session_factory,
            gh=self.gh,
            health=self.health,
            tg=self.tg,
            enricher=self.enricher,
        )
        self.poller = IssuePoller(self.services)
        self.sync_job = RepoSyncJob(self.services)
        self.refresh_job = StateRefreshJob(self.services)
        self.digest_job = DigestFlushJob(self.services)
        self.maintenance_job = MaintenanceJob(self.services)
        self._health_server = None
        self.shutdown_event = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle

    async def startup(self) -> None:
        await ensure_schema(self.engine)
        if not await self.lock.acquire(timeout_seconds=float(self.cfg.ops.worker_lock_timeout_seconds)):
            raise RuntimeError(
                "another optyra worker still holds the advisory lock on this database "
                f"after waiting {self.cfg.ops.worker_lock_timeout_seconds}s"
            )
        await self._seed_orgs()
        self._health_server = start_health_server(
            self.health, self.cfg.ops.healthz_host, self.cfg.ops.healthz_port
        )
        logger.info(
            "optyra started: %s org(s), healthz on :%s, telegram=%s, ai=%s",
            len(self.cfg.orgs),
            self.cfg.ops.healthz_port,
            "on" if self.tg else "off",
            "on" if self.enricher else "off",
        )

    async def shutdown(self) -> None:
        for closer in (
            self.gh.aclose(),
            self.enricher.aclose() if self.enricher else _noop(),
            self.tg.aclose() if self.tg else _noop(),
            self.pinger.aclose(),
            self.lock.release(),
            self.engine.dispose(),
        ):
            try:
                await closer
            except Exception:
                logger.debug("shutdown close failed", exc_info=True)
        if self._health_server is not None:
            self._health_server.shutdown()

    async def _seed_orgs(self) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                dal = DAL(session)
                for entry in self.cfg.orgs:
                    await dal.upsert_org(entry.login, entry.tier, entry.gsoc_years)

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        await self.startup()
        try:
            async with asyncio.TaskGroup() as tg:
                workers = [
                    tg.create_task(self.poller.run_forever(), name="poller"),
                    tg.create_task(self.sync_job.run_forever(), name="repo-sync"),
                    tg.create_task(self.refresh_job.run_forever(), name="state-refresh"),
                    tg.create_task(self.digest_job.run_forever(), name="digest-flush"),
                    tg.create_task(self.maintenance_job.run_forever(), name="maintenance"),
                    tg.create_task(self._ping_loop(), name="healthcheck-ping"),
                ]
                tg.create_task(self._shutdown_watcher(workers), name="shutdown-signal")
        finally:
            await self.shutdown()

    async def _shutdown_watcher(self, workers: list[asyncio.Task]) -> None:
        """On SIGTERM/SIGINT (Render deploy overlap, `docker stop`, Ctrl-C),
        cancel the job tasks so `run()` exits through `shutdown()` — which
        releases the advisory lock for the incoming instance."""
        await self.shutdown_event.wait()
        logger.info("shutdown signal received; stopping %d job task(s)", len(workers))
        for task in workers:
            task.cancel()

    async def _ping_loop(self) -> None:
        """Ping Healthchecks.io when sweeps are productive (dead-man switch, report §16)."""
        while True:
            await asyncio.sleep(120.0)
            last = self.health.last_sweep_at
            if last is None:
                continue
            age = (utcnow_aware() - datetime_from_iso(last)).total_seconds()
            if age < 600:
                await self.pinger.ping(ok=self.health.last_sweep_errors == 0)


async def _noop() -> None:
    return None


def datetime_from_iso(value: str):
    from datetime import datetime

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """Wire SIGINT/SIGTERM to the app's shutdown event.

    PaaS hosts (Render, Fly, Heroku) send SIGTERM to the old instance during
    overlapping deploys; handling it releases the advisory lock promptly so
    the incoming instance can take over instead of timing out on it.
    """
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            # Windows: default SIGINT -> KeyboardInterrupt is enough
            pass


async def run() -> None:
    cfg = load_config()
    setup_logging(cfg.secrets.log_level, cfg.secrets.log_json)
    # secret scrubbing (report §15): register every secret so no log line can leak them
    SCRUBBER.register(cfg.secrets.gh_token)
    SCRUBBER.register(cfg.secrets.telegram_bot_token)
    SCRUBBER.register(cfg.secrets.ai_api_key)
    logger.info(
        "loaded config from %s (overlap=%ss, tier1=%ss, tier2=%ss)",
        cfg.config_dir,
        cfg.poll.overlap_seconds,
        cfg.poll.tier1_interval_seconds,
        cfg.poll.tier2_interval_seconds,
    )
    app = App(cfg)
    install_signal_handlers(app.shutdown_event)
    await app.run()
