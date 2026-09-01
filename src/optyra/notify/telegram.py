"""Telegram delivery (report 01prd §9, 02prd §7).

- HTML messages + inline "Open Issue" button; links never show a preview.
- chat_id allowlist: we only ever push to configured chats and never read incoming
  messages, so strangers who discover the bot get nothing.
- Instant for score >= instant_threshold; everything else queues for the digest flush.
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
    """issue_ctx: issue_key, html_url, title, score, ai_summary, ai_worth_attempting,
    ai_reason_codes, ai_difficulty, labels, stars, gsoc_score."""
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
    meta_bits = []
    labels = issue_ctx.get("labels") or []
    if labels:
        meta_bits.append("🏷 " + esc(", ".join(labels[:4])))
    stars = issue_ctx.get("stars")
    if stars:
        meta_bits.append(f"⭐ {stars:,}")
    gsoc = issue_ctx.get("gsoc_score")
    if gsoc:
        meta_bits.append(f"🎯 GSoC {gsoc}")
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


def format_digest(items: list[dict]) -> list[str]:
    """One ranked digest -> one or more <=4096-char messages. items sorted by score desc."""
    header = f"📬 <b>Optyra digest</b> — {len(items)} new issue(s), ranked by score\n"
    chunks: list[str] = []
    current = header
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
        if len(current) + len(block) > _TG_SAFE_LENGTH and current != header:
            chunks.append(current)
            current = header.replace("Optyra digest", "Optyra digest (cont.)")
        current += block
    if current.strip() != header.strip():
        chunks.append(current)
    return chunks or [header]


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
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "Open Issue", "url": button_url}]]
            }
        ok = True
        for chat_id in self.chat_ids:
            ok = await self._send_one(chat_id, payload) and ok
        return ok

    async def _send_one(self, chat_id: int, payload: dict[str, Any]) -> bool:
        for attempt in (1, 2):
            try:
                response = await self._client.post(
                    "/sendMessage", json={**payload, "chat_id": chat_id}
                )
            except httpx.HTTPError as exc:
                logger.warning("telegram send failed (chat %s): %r", chat_id, exc)
                return False
            if response.status_code == 200:
                return True
            if response.status_code == 429 and attempt == 1:
                retry_after = 1.0
                try:
                    retry_after = float(
                        response.json().get("parameters", {}).get("retry_after", 1.0)
                    )
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

    async def send_digest(self, items: list[dict]) -> int:
        """Returns the number of chunks successfully delivered."""
        delivered = 0
        for chunk in format_digest(items):
            if await self.send_message(chunk):
                delivered += 1
        return delivered
