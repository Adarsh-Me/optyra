"""Logging setup with secret scrubbing (report 01prd §15: tokens must never reach logs)."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class SecretScrubber:
    """Replaces any occurrence of configured secret values in log output with a mask."""

    def __init__(self) -> None:
        self._secrets: tuple[str, ...] = ()

    def register(self, value: str | None) -> None:
        if value and len(value) >= 8 and value not in self._secrets:
            self._secrets = (*self._secrets, value)

    def scrub(self, message: str) -> str:
        for secret in self._secrets:
            if secret in message:
                message = message.replace(secret, "***REDACTED***")
        return message


SCRUBBER = SecretScrubber()


class _ScrubbingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = SCRUBBER.scrub(str(record.msg))
        if record.args:
            try:
                record.args = tuple(
                    SCRUBBER.scrub(str(a)) if isinstance(a, str) else a for a in record.args
                )
            except Exception:  # never let logging itself crash the app
                pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", json_mode: bool | None = None) -> None:
    """Configure the root logger once. JSON mode via LOG_JSON=1 for production."""
    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    if json_mode is None:
        json_mode = os.environ.get("LOG_JSON", "").lower() in ("1", "true", "yes")

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_ScrubbingFilter())
    handler.setFormatter(JsonFormatter() if json_mode else logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level_name)
    logging.getLogger("httpx").setLevel(max(logging.INFO, root.level - 5))
    logging.getLogger("httpcore").setLevel(logging.WARNING)
