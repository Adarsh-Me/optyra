"""Config loader tests."""

from __future__ import annotations

import pytest

from optyra.config import ConfigError, load_config


def test_load_real_config(cfg):
    assert cfg.secrets.gh_token == "test-gh-token-1234567890abcdef"
    assert cfg.secrets.telegram_chat_ids == (111,)
    assert cfg.notify.instant_threshold == 85
    assert cfg.notify.digest_threshold == 70
    assert cfg.poll.tier1_interval_seconds == 180
    assert cfg.poll.tier2_interval_seconds == 720
    assert cfg.poll.overlap_seconds == 120
    assert cfg.poll.max_catchup_hours == 72
    assert len(cfg.orgs) >= 10
    tier1 = [o for o in cfg.orgs if o.tier == 1]
    assert tier1 and all(o.gsoc_years for o in cfg.orgs)
    # recency table parsed ascending by seconds
    ages = [age for age, _ in cfg.scoring.recency]
    assert ages == sorted(ages)
    assert cfg.scoring.recency[0] == (1800, 25)
    # stars table descending
    mins = [m for m, _ in cfg.scoring.stars]
    assert mins == sorted(mins, reverse=True)
    # criteria contract has the report's reason codes
    assert {"good-fit", "env-heavy", "huge-setup", "unclear"} <= set(cfg.ai_criteria["allowed_reason_codes"])


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        load_config("config")


def test_missing_config_dir_raises(tmp_path):
    with pytest.raises(ConfigError, match="missing config file"):
        load_config(tmp_path)


def test_duplicate_org_rejected(tmp_path, monkeypatch):
    import shutil

    shutil.copytree("config", tmp_path / "config")
    orgs = tmp_path / "config" / "orgs.yaml"
    orgs.write_text(
        orgs.read_text(encoding="utf-8") + "  - login: APACHE\n    tier: 2\n    gsoc_years: [2024]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GH_TOKEN", "x" * 20)
    with pytest.raises(ConfigError, match="duplicate org"):
        load_config(tmp_path / "config")


def test_invalid_tier_rejected(tmp_path, monkeypatch):
    import shutil

    shutil.copytree("config", tmp_path / "config")
    orgs = tmp_path / "config" / "orgs.yaml"
    orgs.write_text(orgs.read_text(encoding="utf-8").replace("tier: 1", "tier: 9", 1))
    monkeypatch.setenv("GH_TOKEN", "x" * 20)
    with pytest.raises(ConfigError, match="tier must be 1 or 2"):
        load_config(tmp_path / "config")
