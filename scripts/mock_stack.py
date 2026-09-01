"""One-file fake GitHub + Telegram + Gemini for the Docker smoke test (and reuse).

Serves on 0.0.0.0:9901:
  /repos/... , /search/...        -> canned GitHub world (one candidate issue + noise)
  /bot*/sendMessage               -> records Telegram sends to smoke-tg.log
  /v1beta/models/...:generateContent -> canned Gemini JSON summary

Run: python scripts/mock_stack.py   (then point GITHUB_API_BASE / telegram api_base /
Gemini base at it — see deploy or the smoke-test runbook section in README.)
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(__file__).parent / "smoke-tg.log"

NOW = datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


CANDIDATE = {
    "id": 900001,
    "number": 77,
    "title": "Connector crashes with NPE when custom partitioner is set",
    "state": "open",
    "created_at": iso(NOW - timedelta(minutes=3)),
    "user": {"login": "alice"},
    "labels": [{"name": "good first issue"}],
    "assignees": [],
    "repository_url": "https://api.github.com/repos/acme/widgets",
    "html_url": "https://github.com/acme/widgets/issues/77",
    "body": (
        "When running with a custom partitioner the connector crashes.\n"
        "Steps:\n1. start broker\n2. submit config\n3. observe crash\n"
        "```\njava.lang.NullPointerException at Broker.scala:120\n```\n"
        "Expected: no crash. Reproduces every time on trunk."
    ),
}

NOISE_ASSIGNED = dict(
    CANDIDATE,
    id=900002,
    number=78,
    title="Assigned thing",
    created_at=iso(NOW - timedelta(minutes=6)),
    assignees=[{"login": "bob"}],
    labels=[{"name": "bug"}],
    html_url="https://github.com/acme/widgets/issues/78",
)

REPO = {
    "id": 555,
    "full_name": "acme/widgets",
    "owner": {"login": "acme"},
    "stargazers_count": 28000,
    "language": "Python",
    "archived": False,
    "pushed_at": iso(NOW - timedelta(days=1)),
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/search/repositories":
            self._json({"total_count": 1, "items": [REPO]})
        elif path == "/search/issues":
            self._json({"total_count": 2, "items": [CANDIDATE, NOISE_ASSIGNED]})
        elif path == "/repos/acme/widgets/issues/77":
            self._json(CANDIDATE)
        elif path == "/repos/acme/widgets/issues/77/timeline":
            self._json(
                [
                    {
                        "event": "commented",
                        "created_at": iso(NOW - timedelta(minutes=1)),
                        "actor": {"login": "maintainer1"},
                    },
                ]
            )
        elif path == "/repos/acme/widgets":
            self._json(REPO)
        else:
            self._json({"message": "Not Found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if "/sendMessage" in self.path:
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._json({"ok": True, "result": {"message_id": 1}})
        elif ":generateContent" in self.path:
            self._json(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "summary": "Connector NPE with custom partitioner; null-check fix in rebalance path.",
                                                "worth_attempting": True,
                                                "reason_codes": ["good-fit"],
                                                "difficulty": "medium",
                                            }
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
        else:
            self._json({"error": "unrouted"}, 404)

    def log_message(self, *args) -> None:
        return


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9901
    print(f"mock stack on :{port} (github + telegram + gemini)")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
