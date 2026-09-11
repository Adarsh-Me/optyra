"""LLM enrichment (report 02prd §4): Gemini Flash, candidates only, strict JSON, fail-open.

Pipeline position: poll → hard filters → whitelist → rule score >= threshold → deep-check
→ LLM enrich (timeout 20s, retries, strict JSON) → store cached -> Telegram message.
The LLM must NEVER gate or delay discovery: every failure path returns None and the
notification still goes out without a summary.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com"

ALLOWED_DIFFICULTY = ("easy", "medium", "hard", "unclear")
ALLOWED_SETUP = ("minimal", "moderate", "heavy")


@dataclass
class Enrichment:
    summary: str
    worth_attempting: bool
    reason_codes: list[str]
    difficulty: str
    setup_weight: str = "moderate"  # v2: absent/invalid fails to the middle value
    setup_reason: str | None = None


def _build_system_prompt(criteria: dict) -> str:
    allowed = ", ".join(criteria.get("allowed_reason_codes", []))
    rules = "\n".join(f"- {r}" for r in criteria.get("rules", []))
    schema = criteria.get("output_schema", {})
    schema_lines = "\n".join(f"  {k}: {v}" for k, v in schema.items())
    setup_raw = criteria.get("setup_scale") or {}
    setup_lines = "\n".join(f"{name}: {str(desc).strip()}" for name, desc in setup_raw.items())
    setup_block = f"Setup weight scale (v2 filter):\n{setup_lines}\n\n" if setup_lines else ""
    return (
        f"{criteria.get('task', '').strip()}\n\n"
        f"Output JSON schema:\n{{\n{schema_lines}\n}}\n\n"
        f"Allowed reason_codes: [{allowed}].\n\n"
        f"{setup_block}"
        f"No-go criteria (mark not worth attempting):\n{criteria.get('no_go_criteria', '').strip()}\n\n"
        f"Contributor preferences:\n{criteria.get('preferences', '').strip()}\n\n"
        f"Rules:\n{rules}"
    )


def build_user_payload(ctx: dict, max_body_chars: int) -> str:
    body = (ctx.get("body") or "")[:max_body_chars]
    payload = {
        "repo": ctx.get("repo"),
        "stars": ctx.get("stars"),
        "language": ctx.get("language"),
        "labels": ctx.get("labels"),
        "assignee": ctx.get("assignee"),
        "linked_pr": ctx.get("linked_pr"),
        "title": ctx.get("title"),
        "body": body,
    }
    return json.dumps(payload, ensure_ascii=False)


def _extract_json_text(response_json: dict) -> str | None:
    try:
        candidates = response_json["candidates"]
        parts = candidates[0]["content"]["parts"]
        for part in parts:
            if "text" in part:
                return part["text"]
    except (KeyError, IndexError, TypeError):
        return None
    return None


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip("` \n\t")


def parse_enrichment(raw: dict, *, allowed_codes: set[str], summary_max_chars: int) -> Enrichment | None:
    """Validate + sanitize one model response. None => invalid (caller retries/fails open)."""
    text = _extract_json_text(raw)
    if not text:
        return None
    try:
        data = json.loads(_strip_fences(text))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    summary = data.get("summary")
    worth = data.get("worth_attempting")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(worth, bool):
        return None
    # Enforce the 2-line contract: collapse whitespace, take first two lines, trim length.
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    summary = " ".join(lines[:2])[:summary_max_chars]
    codes = [
        code for code in (data.get("reason_codes") or []) if isinstance(code, str) and code in allowed_codes
    ][:3]
    difficulty = data.get("difficulty")
    if difficulty not in ALLOWED_DIFFICULTY:
        difficulty = "unclear"
    setup_weight = data.get("setup_weight")
    if setup_weight not in ALLOWED_SETUP:
        setup_weight = "moderate"  # fail toward the middle: never block on a missing field
    setup_reason = data.get("setup_reason")
    if not isinstance(setup_reason, str):
        setup_reason = None
    return Enrichment(
        summary=summary,
        worth_attempting=worth,
        reason_codes=codes,
        difficulty=difficulty,
        setup_weight=setup_weight,
        setup_reason=setup_reason.strip()[:160] if setup_reason else None,
    )


class IssueEnricher:
    def __init__(
        self,
        api_key: str,
        model: str,
        criteria: dict,
        *,
        timeout_seconds: int = 20,
        max_retries: int = 2,
        max_body_chars: int = 3000,
        summary_max_chars: int = 220,
        base_url: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.criteria = criteria
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_body_chars = max_body_chars
        self.summary_max_chars = summary_max_chars
        self.allowed_codes = set(criteria.get("allowed_reason_codes", []))
        self.system_prompt = _build_system_prompt(criteria)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )
        self.url = f"/v1beta/models/{model}:generateContent"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def enrich(self, ctx: dict) -> Enrichment | None:
        """One call per issue. Returns None on any failure — fail open, never gate."""
        user_payload = build_user_payload(ctx, self.max_body_chars)
        last_error = "unknown"
        for attempt in range(1, max(1, self.max_retries) + 1):
            prompt = user_payload
            if attempt > 1:
                prompt = (
                    user_payload + "\n\nIMPORTANT: your previous reply was not valid JSON matching the "
                    'schema. Return ONLY one JSON object: {"summary": str, "worth_attempting": bool, '
                    '"reason_codes": [..], "difficulty": "easy|medium|hard|unclear", '
                    '"setup_weight": "minimal|moderate|heavy", "setup_reason": str}'
                )
            body = {
                "systemInstruction": {"parts": [{"text": self.system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.2,
                },
            }
            try:
                response = await self._client.post(self.url, json=body)
            except (TimeoutError, httpx.HTTPError, OSError) as exc:
                last_error = f"transport: {exc!r}"
                logger.warning("AI enrich transport failure (attempt %s): %r", attempt, exc)
                continue
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}: {response.text[:120]}"
                logger.warning("AI enrich HTTP %s (attempt %s)", response.status_code, attempt)
                continue
            parsed = parse_enrichment(
                response.json(),
                allowed_codes=self.allowed_codes,
                summary_max_chars=self.summary_max_chars,
            )
            if parsed is not None:
                return parsed
            last_error = "invalid JSON payload shape"
        logger.warning("AI enrich failed after retries (%s); failing open", last_error)
        return None
