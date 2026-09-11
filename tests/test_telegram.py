"""Telegram formatting + delivery tests (v2: parked/setup annotation, suppressed footer,
funnel report, 429 retry, chunking, allowlist)."""

from __future__ import annotations

import pytest

from conftest import FakeTelegram
from optyra.notify.telegram import (
    TelegramError,
    TelegramNotifier,
    build_digest,
    format_funnel_report,
    format_instant,
)


def ctx(**overrides):
    base = {
        "issue_key": "acme/widgets#1",
        "html_url": "https://github.com/acme/widgets/issues/1",
        "title": "NPE <in> rebalance & crash",
        "score": 92,
        "labels": ["good first issue", "bug"],
        "stars": 28000,
        "parked": None,
        "ai_summary": "Null check missing <in> rebalance path.",
        "ai_worth_attempting": True,
        "ai_reason_codes": ["good-fit"],
        "ai_difficulty": "medium",
        "ai_setup_weight": "minimal",
    }
    base.update(overrides)
    return base


def test_instant_message_v2_formatting():
    text = format_instant(ctx())
    assert "🔥 <b>92</b>" in text
    assert "&lt;in&gt;" in text  # escaped
    assert "🤖" in text
    assert "✅ Worth attempting · medium" in text
    assert "🛠 setup: minimal" in text
    assert "🎯 GSoC" not in text  # retired
    assert "maintainer-parked" not in text
    parked = format_instant(ctx(parked="needs triage"))
    assert "⚠️ maintainer-parked (needs triage)" in parked


async def test_instant_delivery_button_and_payload(cfg):
    tg = FakeTelegram()
    notifier = TelegramNotifier(
        cfg.secrets.telegram_bot_token,
        list(cfg.secrets.telegram_chat_ids),
        transport=tg.transport(),
    )
    assert await notifier.send_message(format_instant(ctx()), button_url=ctx()["html_url"]) is True
    payload = tg.sent[0]
    assert payload["chat_id"] == 111
    assert payload["parse_mode"] == "HTML"
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/issues/1")
    assert payload["link_preview_options"] == {"is_disabled": True}


async def test_verdict_variants():
    assert "⚠️ Not recommended — env heavy" in format_instant(
        ctx(ai_worth_attempting=False, ai_reason_codes=["env-heavy"])
    )
    assert "AI unavailable" in format_instant(
        ctx(ai_worth_attempting=None, ai_summary=None, ai_setup_weight=None)
    )
    assert "🛠 setup: heavy" in format_instant(ctx(ai_setup_weight="heavy"))


def test_digest_chunking_and_membership():
    items = [
        {
            "issue_key": f"acme/widgets#{i}",
            "html_url": f"https://github.com/acme/widgets/issues/{i}",
            "title": f"Issue number {i} with a reasonably long title for chunk testing",
            "score": 80,
            "parked": None,
            "ai_summary": "Summary text.",
            "ai_worth_attempting": True,
            "ai_reason_codes": ["good-fit"],
            "ai_difficulty": "easy",
            "ai_setup_weight": None,
        }
        for i in range(60)
    ]
    chunks = build_digest(items)
    assert len(chunks) > 1
    covered = [item for _, items_in_chunk in chunks for item in items_in_chunk]
    assert len(covered) == 60  # no item lost
    for text, _ in chunks:
        assert len(text) <= 4096


def test_digest_suppressed_footer():
    items = [
        {
            "issue_key": "a/b#1",
            "html_url": "u",
            "title": "t",
            "score": 75,
            "ai_summary": None,
            "ai_worth_attempting": None,
            "ai_reason_codes": [],
            "ai_difficulty": None,
            "ai_setup_weight": None,
            "parked": None,
        }
    ]
    chunks = build_digest(items, suppressed_note=7)
    assert "7 more suppressed today" in chunks[-1][0]


def test_funnel_report_formatting():
    funnel = {
        "seen": 42,
        "hard_filter": 20,
        "below_threshold": 15,
        "setup_dropped": 3,
        "lane_gate": 2,
        "ai_calls": 6,
        "notified_instant": 1,
        "queued_digest": 2,
        "budget_suppressed": 1,
        "owner:acme:seen": 40,
        "owner:acme:notified": 2,
        "owner:other:seen": 2,
    }
    text = format_funnel_report("2026-09-03", funnel, config_hash="abcd1234")
    assert "issues seen (new): <b>42</b>" in text
    assert "cfg abcd1234" in text
    assert "acme: 40 / <b>2</b>" in text
    assert "owner:acme:seen" not in text  # rendered human-readable only


async def test_429_retry_after(cfg):
    tg = FakeTelegram(status_sequence=[429, 200])
    notifier = TelegramNotifier(
        cfg.secrets.telegram_bot_token,
        list(cfg.secrets.telegram_chat_ids),
        transport=tg.transport(),
        sleep=tg.sleep,
    )
    assert await notifier.send_message("hello") is True
    assert tg.sleeps == [3.0]
    assert len(tg.sent) == 2


async def test_rejection_returns_false(cfg):
    tg = FakeTelegram(status_sequence=[400, 400])
    notifier = TelegramNotifier(
        cfg.secrets.telegram_bot_token,
        list(cfg.secrets.telegram_chat_ids),
        transport=tg.transport(),
        sleep=tg.sleep,
    )
    assert await notifier.send_message("hello") is False


def test_allowlist_mandatory():
    with pytest.raises(TelegramError):
        TelegramNotifier("token", [])
