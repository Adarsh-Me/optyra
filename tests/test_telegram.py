"""Telegram formatting + delivery tests."""

from __future__ import annotations

import pytest

from conftest import FakeTelegram


def ctx(**overrides):
    base = {
        "issue_key": "apache/kafka#1",
        "html_url": "https://github.com/apache/kafka/issues/1",
        "title": "NPE <in> rebalance & crash",
        "score": 95,
        "labels": ["good first issue", "bug"],
        "stars": 28000,
        "gsoc_score": 80,
        "ai_summary": "Null check missing <in> rebalance path.",
        "ai_worth_attempting": True,
        "ai_reason_codes": ["good-fit"],
        "ai_difficulty": "medium",
    }
    base.update(overrides)
    return base


async def test_instant_message_html_and_button(cfg, FakeTgFactory=None):
    from optyra.notify.telegram import format_instant

    text = format_instant(ctx())
    assert "🔥 <b>95</b>" in text
    assert "&lt;in&gt;" in text  # escaped
    assert "🤖" in text
    assert "✅ Worth attempting · medium" in text
    assert "🎯 GSoC 80" in text

    tg = FakeTelegram()
    from optyra.notify.telegram import TelegramNotifier

    notifier = TelegramNotifier(
        cfg.secrets.telegram_bot_token,
        list(cfg.secrets.telegram_chat_ids),
        transport=tg.transport(),
    )
    assert await notifier.send_message(text, button_url=ctx()["html_url"]) is True
    payload = tg.sent[0]
    assert payload["chat_id"] == 111
    assert payload["parse_mode"] == "HTML"
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/issues/1")
    assert payload["link_preview_options"] == {"is_disabled": True}


async def test_verdict_negative(cfg):
    from optyra.notify.telegram import format_instant

    text = format_instant(ctx(ai_worth_attempting=False, ai_reason_codes=["env-heavy"]))
    assert "⚠️ Not recommended — env heavy" in text


async def test_verdict_missing_is_annotated(cfg):
    from optyra.notify.telegram import format_instant

    text = format_instant(ctx(ai_worth_attempting=None, ai_summary=None))
    assert "AI unavailable" in text


async def test_digest_chunking():
    from optyra.notify.telegram import build_digest

    items = [
        {
            "issue_key": f"apache/kafka#{i}",
            "html_url": f"https://github.com/apache/kafka/issues/{i}",
            "title": f"Issue number {i} with a reasonably long title for chunk testing",
            "score": 80,
            "ai_summary": "Summary text.",
            "ai_worth_attempting": True,
            "ai_reason_codes": ["good-fit"],
            "ai_difficulty": "easy",
        }
        for i in range(60)
    ]
    chunks = build_digest(items)
    assert len(chunks) > 1
    covered = [item for _, items_in_chunk in chunks for item in items_in_chunk]
    assert len(covered) == 60  # no item lost
    for text, _ in chunks:
        assert len(text) <= 4096


async def test_429_retry_after(cfg):
    tg = FakeTelegram(status_sequence=[429, 200])
    from optyra.notify.telegram import TelegramNotifier

    notifier = TelegramNotifier(
        cfg.secrets.telegram_bot_token,
        list(cfg.secrets.telegram_chat_ids),
        transport=tg.transport(),
        sleep=tg.sleep,
    )
    assert await notifier.send_message("hello") is True
    assert tg.sleeps == [3.0]  # retry_after honored
    assert len(tg.sent) == 2


async def test_rejection_returns_false(cfg):
    tg = FakeTelegram(status_sequence=[400, 400])
    from optyra.notify.telegram import TelegramNotifier

    notifier = TelegramNotifier(
        cfg.secrets.telegram_bot_token,
        list(cfg.secrets.telegram_chat_ids),
        transport=tg.transport(),
        sleep=tg.sleep,
    )
    assert await notifier.send_message("hello") is False


def test_allowlist_mandatory():
    from optyra.notify.telegram import TelegramError, TelegramNotifier

    with pytest.raises(TelegramError):
        TelegramNotifier("token", [])
