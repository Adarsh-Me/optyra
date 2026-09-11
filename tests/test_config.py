"""Config loader tests (v2 watch-set schema)."""

from __future__ import annotations

import shutil

import pytest

from optyra.config import ConfigError, load_config


def test_load_real_config(cfg):
    assert cfg.secrets.gh_token == "test-gh-token-1234567890abcdef"
    assert cfg.secrets.telegram_chat_ids == (111,)
    # v2 watch set (identifiers verified against live GitHub)
    assert len(cfg.watch.orgs) == 16
    for expected in (
        "tensorflow",
        "godotengine",
        "google-gemini",
        "swiftlang",
        "jenkinsci",
        "NixOS",
        "dart-lang",
    ):
        assert expected in cfg.watch.orgs, expected
    assert "apache" not in cfg.watch.orgs  # v1 orgs retired
    assert "pytorch" not in cfg.watch.orgs and "rust-lang" not in cfg.watch.orgs
    assert cfg.watch.pinned_repos == ("laurent22/joplin", "NixOS/nixpkgs", "dart-lang/sdk")
    assert cfg.watch.blocked_owners == frozenset({"pytorch", "rust-lang"})
    # gate + polling
    assert cfg.sync.min_stars == 10000
    assert cfg.sync.interval_hours == 6
    assert cfg.poll.interval_seconds == 180
    assert cfg.poll.overlap_seconds == 120
    assert cfg.poll.max_catchup_hours == 72
    # thresholds + caps
    assert cfg.notify.instant_threshold == 85 and cfg.notify.digest_threshold == 70
    assert cfg.notify.per_owner_daily_cap == 5
    assert cfg.notify.digest_max_items == 20
    assert cfg.notify.lane_signal_required is True
    assert cfg.notify.instant_lane_bypasses_gate is True
    # scoring recalibration
    ages = [age for age, _ in cfg.scoring.recency]
    assert ages == sorted(ages)
    assert cfg.scoring.recency[0] == (1800, 25)
    mins = [m for m, _ in cfg.scoring.stars]
    assert mins == sorted(mins, reverse=True) == [50000, 20000, 10000]
    assert cfg.scoring.label_points_cap == 20
    assert cfg.scoring.parked_penalty == 8
    assert cfg.scoring.setup_minimal_bonus == 5
    assert cfg.scoring.repo_pushed_days == 3
    # filters: hard negatives include v2 additions; soft labels carry the parked family
    assert {"question", "stale", "upstream"} <= set(cfg.filters.negative_labels)
    assert "needs triage" in cfg.filters.soft_labels
    # AI setup policy
    assert cfg.ai.setup_filter_default == "hard"
    assert cfg.ai.org_setup_filter["llvm"] == "hard"
    assert cfg.ai.prior_min_samples == 5 and cfg.ai.daily_call_cap == 300
    assert cfg.ai.keyword_screen is False
    # funnel report
    assert cfg.funnel.report_hour_utc == 21 and cfg.funnel.report_enabled is True
    # criteria contract v2
    assert "setup_weight" in cfg.ai_criteria["output_schema"]
    assert set(cfg.ai_criteria["setup_scale"]) == {"minimal", "moderate", "heavy"}
    assert {"good-fit", "env-heavy", "huge-setup", "unclear"} <= set(cfg.ai_criteria["allowed_reason_codes"])
    # config hash for split-brain visibility
    assert len(cfg.config_hash) == 8
    int(cfg.config_hash, 16)


def test_hash_changes_with_config(tmp_path, monkeypatch):
    shutil.copytree("config", tmp_path / "config")
    monkeypatch.setenv("GH_TOKEN", "x" * 20)
    a = load_config(tmp_path / "config").config_hash
    orgs = tmp_path / "config" / "orgs.yaml"
    orgs.write_text(orgs.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")
    b = load_config(tmp_path / "config").config_hash
    assert a != b


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        load_config("config")


def test_missing_config_dir_raises(tmp_path):
    with pytest.raises(ConfigError, match="missing config file"):
        load_config(tmp_path)


def test_watch_section_required(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x" * 20)
    shutil.copytree("config", tmp_path / "config")
    (tmp_path / "config" / "orgs.yaml").write_text("orgs:\n  - login: apache\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="'watch' mapping"):
        load_config(tmp_path / "config")


def test_duplicate_org_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x" * 20)
    shutil.copytree("config", tmp_path / "config")
    orgs = tmp_path / "config" / "orgs.yaml"
    orgs.write_text(
        orgs.read_text(encoding="utf-8").replace("    - electron", "    - electron\n    - TENSORFLOW", 1),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate org"):
        load_config(tmp_path / "config")


def test_bad_pinned_format_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x" * 20)
    shutil.copytree("config", tmp_path / "config")
    orgs = tmp_path / "config" / "orgs.yaml"
    orgs.write_text(
        orgs.read_text(encoding="utf-8").replace("- laurent22/joplin", "- laurent22"), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(tmp_path / "config")


def test_pinned_blocked_owner_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x" * 20)
    shutil.copytree("config", tmp_path / "config")
    orgs = tmp_path / "config" / "orgs.yaml"
    orgs.write_text(
        orgs.read_text(encoding="utf-8").replace(
            "- NixOS/nixpkgs", "- rust-lang/whatever\n    - NixOS/nixpkgs"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="blocked owner"):
        load_config(tmp_path / "config")


def test_invalid_setup_mode_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x" * 20)
    shutil.copytree("config", tmp_path / "config")
    conf = tmp_path / "config" / "config.yaml"
    conf.write_text(
        conf.read_text(encoding="utf-8").replace("setup_filter_default: hard", "setup_filter_default: maybe"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must be 'hard' or 'soft'"):
        load_config(tmp_path / "config")


def test_port_env_overrides_healthz(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x" * 20)
    monkeypatch.setenv("PORT", "1234")
    loaded = load_config("config")
    assert loaded.ops.healthz_port == 1234
