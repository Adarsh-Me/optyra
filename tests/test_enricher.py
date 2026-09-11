"""AI enricher tests (v2): strict parse incl. setup_weight, repair retry, fail-open,
sanitizing, prompt carries the setup calibration."""

from __future__ import annotations

import json

from conftest import FakeAI, ai_response
from optyra.ai.enricher import IssueEnricher, _build_system_prompt


def make_enricher(cfg, fake: FakeAI):
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
    "setup_weight": "minimal",
}


def issue_ctx():
    return {
        "repo": "acme/widgets",
        "stars": 28000,
        "language": "Java",
        "labels": ["good first issue"],
        "assignee": None,
        "linked_pr": False,
        "title": "NPE in rebalance",
        "body": "Long body " * 50,
    }


async def test_valid_response_parsed_with_setup(cfg):
    enricher = make_enricher(cfg, FakeAI([ai_response(GOOD)]))
    result = await enricher.enrich(issue_ctx())
    assert result is not None
    assert result.worth_attempting is True
    assert result.setup_weight == "minimal"
    assert "NPE" in result.summary


async def test_setup_weight_defaults_to_moderate(cfg):
    payload = dict(GOOD)
    payload.pop("setup_weight")
    enricher = make_enricher(cfg, FakeAI([ai_response(payload)]))
    result = await enricher.enrich(issue_ctx())
    assert result is not None and result.setup_weight == "moderate"


async def test_invalid_setup_falls_back(cfg):
    payload = dict(GOOD, setup_weight="impossible-thing")
    enricher = make_enricher(cfg, FakeAI([ai_response(payload)]))
    result = await enricher.enrich(issue_ctx())
    assert result is not None and result.setup_weight == "moderate"


async def test_heavy_parsed(cfg):
    payload = dict(GOOD, setup_weight="heavy", setup_reason="needs Xcode + iOS device")
    enricher = make_enricher(cfg, FakeAI([ai_response(payload)]))
    result = await enricher.enrich(issue_ctx())
    assert result.setup_weight == "heavy"
    assert result.setup_reason == "needs Xcode + iOS device"


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
    assert fake.calls == 2


async def test_http_errors_fail_open(cfg):
    fake = FakeAI([500, 500])
    enricher = make_enricher(cfg, fake)
    assert await enricher.enrich(issue_ctx()) is None


async def test_reason_codes_filtered_and_difficulty_fallback(cfg):
    weird = dict(
        GOOD,
        reason_codes=["good-fit", "made-up-code", "env-heavy"],
        difficulty="impossible",
    )
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


def test_system_prompt_embeds_criteria_and_setup_scale(cfg):
    prompt = _build_system_prompt(cfg.ai_criteria)
    assert "GPU" in prompt
    assert "Windows-only" in prompt
    assert "reason_codes" in prompt
    # v2: the calibration block must be in the prompt
    assert "minimal" in prompt and "Xcode" in prompt
    assert "build environments differ" not in prompt  # that's DAL docs, not the model's
