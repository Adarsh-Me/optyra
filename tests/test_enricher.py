"""AI enricher tests: strict parsing, repair retry, fail-open (report 02prd §4)."""

from __future__ import annotations

import json

from conftest import FakeAI, ai_response


def make_enricher(cfg, fake: FakeAI):
    from optyra.ai.enricher import IssueEnricher

    return IssueEnricher(
        api_key=cfg.secrets.ai_api_key,
        model=cfg.ai.model,
        criteria=cfg.ai_criteria,
        timeout_seconds=cfg.ai.timeout_seconds,
        max_retries=cfg.ai.max_retries,
        max_body_chars=cfg.ai.max_body_chars,
        summary_max_chars=cfg.ai.summary_max_chars,
        transport=fake.transport(),
    )


GOOD = {
    "summary": "Connector crashes with NPE when a custom partitioner is set; fix is a null check.",
    "worth_attempting": True,
    "reason_codes": ["good-fit"],
    "difficulty": "medium",
}


def issue_ctx():
    return {
        "repo": "apache/kafka",
        "stars": 28000,
        "language": "Java",
        "labels": ["good first issue"],
        "assignee": None,
        "linked_pr": False,
        "title": "NPE in rebalance",
        "body": "Long body " * 50,
    }


async def test_valid_response_parsed(cfg):
    enricher = make_enricher(cfg, FakeAI([ai_response(GOOD)]))
    result = await enricher.enrich(issue_ctx())
    assert result is not None
    assert result.worth_attempting is True
    assert result.reason_codes == ["good-fit"]
    assert result.difficulty == "medium"
    assert "NPE" in result.summary


async def test_markdown_fences_repaired(cfg):
    fenced = {"candidates": [{"content": {"parts": [{"text": "```json\n" + json.dumps(GOOD) + "\n```"}]}}]}
    enricher = make_enricher(cfg, FakeAI([fenced]))
    result = await enricher.enrich(issue_ctx())
    assert result is not None and result.worth_attempting is True


async def test_invalid_then_valid_uses_repair_retry(cfg):
    fake = FakeAI([ai_response({"summary": 42}), ai_response(GOOD)])
    enricher = make_enricher(cfg, fake)
    result = await enricher.enrich(issue_ctx())
    assert result is not None and fake.calls == 2


async def test_garbage_always_fails_open(cfg):
    fake = FakeAI([ai_response({"nonsense": True}), ai_response({"nonsense": True})])
    enricher = make_enricher(cfg, fake)
    assert await enricher.enrich(issue_ctx()) is None
    assert fake.calls == 2  # max_retries honored, no crash


async def test_http_errors_fail_open(cfg):
    fake = FakeAI([500, 500])
    enricher = make_enricher(cfg, fake)
    assert await enricher.enrich(issue_ctx()) is None


async def test_reason_codes_filtered_and_difficulty_fallback(cfg):
    weird = dict(GOOD, reason_codes=["good-fit", "made-up-code", "env-heavy"], difficulty="impossible")
    enricher = make_enricher(cfg, FakeAI([ai_response(weird)]))
    result = await enricher.enrich(issue_ctx())
    assert result is not None
    assert result.reason_codes == ["good-fit", "env-heavy"]
    assert result.difficulty == "unclear"


async def test_summary_clamped_to_two_lines(cfg):
    long = dict(GOOD, summary="line one\nline two\nline three\n" + "x" * 500)
    enricher = make_enricher(cfg, FakeAI([ai_response(long)]))
    result = await enricher.enrich(issue_ctx())
    assert result is not None
    assert "\n" not in result.summary
    assert len(result.summary) <= cfg.ai.summary_max_chars


def test_system_prompt_embeds_criteria(cfg):
    from optyra.ai.enricher import _build_system_prompt

    prompt = _build_system_prompt(cfg.ai_criteria)
    assert "GPU" in prompt
    assert "Windows-only" in prompt
    assert "reason_codes" in prompt
