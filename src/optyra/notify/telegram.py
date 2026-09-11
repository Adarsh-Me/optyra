"""Telegram delivery (report 01prd §9, 02prd §7) — v2.

- HTML messages + inline "Open Issue" button; links never show a preview.
- chat_id allowlist: we only ever push to configured chats and never read incoming
  messages, so strangers who discover the bot get nothing.
- Instant for score >= instant_threshold; everything else queues for the digest flush
  (which enforces the per-owner daily budget and per-flush item cap — suppressed
  overflow is footnoted, never silently dropped).
- v2 message extras: ⚠️ maintainer-parked tag, setup-weight annotation.
- Daily funnel self-report (seen -> gates -> filters -> deep-check -> setup drop ->
  lane gate -> notified -> budget suppressed, per-owner counts, config hash, watchlist).
- 429 responses are honored (parameters.retry_after) with one retry.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TG_MESSAGE_LIMIT = 4096
_TG_SAFE_LENGTH = 3900  # headroom for markup + chunk header


class TelegramError(Exception):
    pass


def esc(text: str | None) -> str:
    return html.escape(str(text or ""), quote=False)


def humanize_reason(code: str) -> str:
    return code.replace("-", " ").replace("_", " ")


def format_instant(issue_ctx: dict) -> str:
    """issue_ctx: issue_key, html_url, title, score, ai_*, labels, stars, parked."""
    parts = [
        f"🔥 <b>{issue_ctx['score']}</b> · "
        f'<a href="{esc(issue_ctx["html_url"])}">{esc(issue_ctx["issue_key"])}</a>',
        f"<b>{esc(issue_ctx['title'])}</b>",
        "",
    ]
    summary = issue_ctx.get("ai_summary")
    if summary:
        parts.append(f"🤖 {esc(summary)}")
    parts.append(_verdict_line(issue_ctx))
    if issue_ctx.get("parked"):
        parts.append(f"⚠️ maintainer-parked ({esc(str(issue_ctx['parked']))})")
    setup = issue_ctx.get("ai_setup_weight")
    if setup and setup != "moderate":
        parts.append(f"🛠 setup: {esc(setup)}")
    meta_bits = []
    labels = issue_ctx.get("labels") or []
    if labels:
        meta_bits.append("🏷 " + esc(", ".join(labels[:4])))
    stars = issue_ctx.get("stars")
    if stars:
        meta_bits.append(f"⭐ {stars:,}")
    if meta_bits:
        parts.append(" · ".join(meta_bits))
    return "\n".join(parts)


def _verdict_line(issue_ctx: dict) -> str:
    worth = issue_ctx.get("ai_worth_attempting")
    difficulty = issue_ctx.get("ai_difficulty")
    codes = issue_ctx.get("ai_reason_codes") or []
    if worth is None:
        return "🧭 <i>AI unavailable — no verdict</i>"
    if worth:
        suffix = f" · {esc(difficulty)}" if difficulty and difficulty != "unclear" else ""
        return f"🧭 ✅ Worth attempting{suffix}"
    reasons = ", ".join(humanize_reason(c) for c in codes[:2]) if codes else "review manually"
    return f"🧭 ⚠️ Not recommended — {esc(reasons)}"


def build_digest(items: list[dict], *, suppressed_note: int = 0) -> list[tuple[str, list[dict]]]:
    """One ranked digest -> list of (message_text, items_in_this_chunk), <=4096 chars.

    Chunk membership is returned so the caller can mark exactly the delivered items.
    `suppressed_note` adds a "+N more suppressed today" footer (budget / cap overflow)."""
    plural = "s" if len(items) != 1 else ""
    header = f"📬 <b>Optyra digest</b> — {len(items)} new issue{plural}, ranked by score\n"
    footer = (
        f"\n➕ {suppressed_note} more suppressed today (owner caps / size cap)" if suppressed_note else ""
    )
    cont_header = "📬 <b>Optyra digest (cont.)</b>\n"
    chunks: list[tuple[str, list[dict]]] = []
    current = header
    current_items: list[dict] = []
    for idx, item in enumerate(items, start=1):
        block = (
            f"\n<b>{idx}. {item['score']}</b> · "
            f'<a href="{esc(item["html_url"])}">{esc(item["issue_key"])}</a>'
            f" — {esc(str(item['title'])[:90])}\n"
        )
        summary = item.get("ai_summary")
        if summary:
            block += f"🤖 {esc(str(summary)[:160])}\n"
        block += _verdict_line(item) + "\n"
        if item.get("parked"):
            block += f"⚠️ maintainer-parked ({esc(str(item['parked']))})\n"
        if len(current) + len(block) + len(footer) > _TG_SAFE_LENGTH and current_items:
            chunks.append((current, current_items))
            current = cont_header
            current_items = []
        current += block
        current_items.append(item)
    if current_items:
        chunks.append((current + footer, current_items))
    elif footer:
        chunks.append((header + footer, []))
    return chunks


def format_digest(items: list[dict]) -> list[str]:
    return [text for text, _ in build_digest(items)]


# ---------------------------------------------------------------- funnel daily report


_FUNNEL_STAGE_LABELS = {
    "seen": "issues seen (new)",
    "gate_rejected": "star-gate rejected",
    "gate_evaluations": "repo gate evaluations",
    "gate_404": "repo fetches 404",
    "hard_filter": "hard filters dropped",
    "below_threshold": "below score threshold",
    "deep_rejected": "deep-check rejects (claimed/closed)",
    "setup_dropped": "AI setup-heavy dropped",
    "lane_gate": "lane gate (no contribution signal)",
    "ai_calls": "Gemini calls",
    "ai_failures": "Gemini failures",
    "queued_digest": "queued for digest",
    "notified_instant": "INSTANT sent",
    "notified_digest": "digest sent",
    "budget_suppressed": "suppressed (owner/size caps)",
}


def format_funnel_report(day: str, funnel: dict, *, config_hash: str) -> str:
    """Daily self-report: tune 70/85 with data, not vibes."""
    lines = [f"📊 <b>Optyra funnel — {esc(day)}</b> (cfg {esc(config_hash)})\n"]
    for key, label in _FUNNEL_STAGE_LABELS.items():
        count = funnel.get(key)
        if count:
            lines.append(f"{label}: <b>{count}</b>")
    owner_notified: dict[str, int] = {}
    owner_seen: dict[str, int] = {}
    for key, value in funnel.items():
        if key.startswith("owner:"):
            _, owner, kind = key.split(":", 2)
            if kind == "seen":
                owner_seen[owner] = int(value)
            elif kind == "notified":
                owner_notified[owner] = int(value)
    owners = sorted(set(owner_notified) | set(owner_seen), key=lambda o: -owner_notified.get(o, 0))
    if owners:
        lines.append("\n<b>per owner (seen/notified):</b>")
        for owner in owners[:10]:
            lines.append(
                f"  {esc(owner)}: {owner_seen.get(owner, 0)} / <b>{owner_notified.get(owner, 0)}</b>"
            )
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_ids: list[int] | tuple[int, ...],
        *,
        api_base: str = "https://api.telegram.org",
        parse_mode: str = "HTML",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        if not chat_ids:
            raise TelegramError("no TELEGRAM_CHAT_ID configured (allowlist is mandatory)")
        self.chat_ids = list(chat_ids)
        self.parse_mode = parse_mode
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=f"{api_base}/bot{bot_token}",
            timeout=httpx.Timeout(timeout),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_message(self, text: str, *, button_url: str | None = None) -> bool:
        """Send to every allowlisted chat. True only if every chat accepted it."""
        payload: dict[str, Any] = {
            "text": text,
            "parse_mode": self.parse_mode,
            "link_preview_options": {"is_disabled": True},
        }
        if button_url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Open Issue", "url": button_url}]]}
        ok = True
        for chat_id in self.chat_ids:
            ok = await self._send_one(chat_id, payload) and ok
        return ok

    async def _send_one(self, chat_id: int, payload: dict[str, Any]) -> bool:
        for attempt in (1, 2):
            try:
                response = await self._client.post("/sendMessage", json={**payload, "chat_id": chat_id})
            except httpx.HTTPError as exc:
                logger.warning("telegram send failed (chat %s): %r", chat_id, exc)
                return False
            if response.status_code == 200:
                return True
            if response.status_code == 429 and attempt == 1:
                retry_after = 1.0
                try:
                    retry_after = float(response.json().get("parameters", {}).get("retry_after", 1.0))
                except Exception:
                    pass
                logger.warning("telegram 429; sleeping %.0fs", retry_after)
                await self._sleep(retry_after)
                continue
            logger.error(
                "telegram send rejected (chat %s): %s %s",
                chat_id,
                response.status_code,
                response.text[:200],
            )
            return False
        return False

    async def send_digest(self, items: list[dict], *, suppressed_note: int = 0) -> int:
        """Returns the number of chunks successfully delivered."""
        delivered = 0
        for text, _ in build_digest(items, suppressed_note=suppressed_note):
            if await self.send_message(text):
                delivered += 1
        return delivered
