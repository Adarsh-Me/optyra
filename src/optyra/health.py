"""Operational visibility: /healthz endpoint + Healthchecks.io dead-man ping (report §16)."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

import optyra


@dataclass
class HealthState:
    """Mutable counters updated by jobs; read-only via /healthz."""

    last_sweep_at: str | None = None
    last_sweep_polled: int = 0
    last_sweep_new_issues: int = 0
    last_sweep_errors: int = 0
    last_sweep_instant: int = 0
    last_sweep_digest: int = 0
    last_sync_at: str | None = None
    last_state_refresh_at: str | None = None
    last_digest_flush_at: str | None = None
    pending_notifications: int = 0
    github_rate_remaining: int | None = None
    extra: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        return {
            "status": "ok",
            "version": optyra.__version__,
            "uptime_seconds": int(time.time() - _STARTED_AT),
            **{k: v for k, v in self.__dict__.items()},
        }


_STARTED_AT = time.time()


class _HealthHandler(BaseHTTPRequestHandler):
    state: HealthState

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.rstrip("/") in ("", "/healthz"):
            body = json.dumps(self.state.snapshot(), default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args) -> None:  # silence per-request stderr noise
        return


def start_health_server(state: HealthState, host: str, port: int) -> ThreadingHTTPServer | None:
    """Daemon-thread HTTP server exposing GET /healthz. port<=0 disables."""
    if port <= 0:
        return None
    handler = type("BoundHealthHandler", (_HealthHandler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class HealthcheckPinger:
    """Dead-man switch: ping Healthchecks.io after each productive sweep (report §16)."""

    def __init__(self, url: str | None, timeout: float = 10.0) -> None:
        self.url = url
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout)) if url else None

    async def ping(self, *, ok: bool = True) -> None:
        if not self._client or not self.url:
            return
        try:
            await self._client.get(self.url if ok else f"{self.url}/fail")
        except httpx.HTTPError as exc:
            import logging

            logging.getLogger(__name__).debug("healthcheck ping failed: %r", exc)

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
