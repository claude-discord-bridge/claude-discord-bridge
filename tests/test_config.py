import pytest
from pathlib import Path

from bridge.config import ConfigError, Settings, load_settings


def test_loads_full_settings(tmp_path):
    env = {
        "DISCORD_TOKEN": "tok-123",
        "OWNER_ID": "42",
        "GUILD_IDS": "7,8",
        "STATE_DIR": str(tmp_path / "state"),
        "DEFAULT_CWD": str(tmp_path),
    }
    s = load_settings(env)
    assert isinstance(s, Settings)
    assert s.token == "tok-123"
    assert s.owner_id == 42
    assert s.guild_ids == frozenset({7, 8})
    assert s.state_dir == tmp_path / "state"
    assert s.idle_timeout_hours == 2.0
    assert s.approval_timeout_s == 120.0


def test_missing_owner_id_is_fatal():
    env = {"DISCORD_TOKEN": "tok-123"}
    with pytest.raises(ConfigError, match="OWNER_ID"):
        load_settings(env)


def test_missing_token_is_fatal():
    env = {"OWNER_ID": "42"}
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_settings(env)


def test_owner_id_must_be_numeric():
    env = {"DISCORD_TOKEN": "tok-123", "OWNER_ID": "not-a-number"}
    with pytest.raises(ConfigError, match="OWNER_ID"):
        load_settings(env)


def test_empty_guild_ids_allowed_for_dm_only():
    env = {"DISCORD_TOKEN": "tok-123", "OWNER_ID": "42"}
    s = load_settings(env)
    assert s.guild_ids == frozenset()


def test_uses_default_values_when_optional_fields_absent():
    env = {"DISCORD_TOKEN": "tok-123", "OWNER_ID": "42"}
    s = load_settings(env)
    assert s.state_dir == Path("~/.claude-discord").expanduser()
    assert s.default_cwd == Path("~").expanduser()
    assert s.idle_timeout_hours == 2.0
    assert s.approval_timeout_s == 120.0


def test_non_positive_owner_id_is_rejected():
    """`OWNER_ID=0` used to parse cleanly and produce a bot comparing every
    author id against 0. The owner check is the one rail the spec calls
    mandatory; it must not be disable-able by a typo."""
    for bad in ("0", "-1"):
        with pytest.raises(ConfigError):
            load_settings({"DISCORD_TOKEN": "t", "OWNER_ID": bad})


def test_sandbox_allowed_domains_default_and_delegate_none():
    env = {"DISCORD_TOKEN": "tok-123", "OWNER_ID": "42"}
    s = load_settings(env)
    assert s.sandbox_allowed_domains == frozenset()
    assert s.delegate is None


def test_delegate_valid_full():
    env = {
        "DISCORD_TOKEN": "tok-123",
        "OWNER_ID": "42",
        "DELEGATE_NAME": "codex",
        "DELEGATE_BIN": "/opt/codex",
        "DELEGATE_ARGS": "--sandbox -p {prompt}",
        "DELEGATE_TIMEOUT_S": "60",
        "DELEGATE_PROTECT_PATHS": "~/.codex, ~/.gemini",
    }
    s = load_settings(env)
    assert s.delegate is not None
    assert s.delegate.name == "codex"
    assert s.delegate.bin == Path("/opt/codex")
    assert s.delegate.args == ("--sandbox", "-p", "{prompt}")
    assert s.delegate.timeout_s == 60.0
    assert s.delegate.protect_paths == ("~/.codex", "~/.gemini")


def test_delegate_defaults_when_minimal():
    env = {
        "DISCORD_TOKEN": "tok-123",
        "OWNER_ID": "42",
        "DELEGATE_NAME": "helper",
        "DELEGATE_BIN": "/opt/helper",
        "DELEGATE_ARGS": "--exec {prompt}",
    }
    s = load_settings(env)
    assert s.delegate is not None
    assert s.delegate.name == "helper"
    assert s.delegate.bin == Path("/opt/helper")
    assert s.delegate.args == ("--exec", "{prompt}")
    assert s.delegate.timeout_s == 3000.0
    assert s.delegate.protect_paths == ()


def test_delegate_bin_relative_fails_but_tilde_expands():
    env_rel = {
        "DISCORD_TOKEN": "tok-123",
        "OWNER_ID": "42",
        "DELEGATE_NAME": "helper",
        "DELEGATE_BIN": "relative/path/helper",
        "DELEGATE_ARGS": "-p {prompt}",
    }
    with pytest.raises(ConfigError, match="DELEGATE_BIN"):
        load_settings(env_rel)

    env_tilde = {
        "DISCORD_TOKEN": "tok-123",
        "OWNER_ID": "42",
        "DELEGATE_NAME": "helper",
        "DELEGATE_BIN": "~/helper",
        "DELEGATE_ARGS": "-p {prompt}",
    }
    s = load_settings(env_tilde)
    assert s.delegate is not None
    assert s.delegate.bin == Path("~/helper").expanduser()


def test_delegate_args_prompt_validation():
    base = {
        "DISCORD_TOKEN": "tok-123",
        "OWNER_ID": "42",
        "DELEGATE_NAME": "helper",
        "DELEGATE_BIN": "/opt/helper",
    }
    # missing {prompt}
    with pytest.raises(ConfigError, match="DELEGATE_ARGS"):
        load_settings({**base, "DELEGATE_ARGS": "--run"})

    # 2 {prompt} tokens
    with pytest.raises(ConfigError, match="DELEGATE_ARGS"):
        load_settings({**base, "DELEGATE_ARGS": "-p {prompt} -p {prompt}"})

    # substring {prompt}
    with pytest.raises(ConfigError, match="DELEGATE_ARGS"):
        load_settings({**base, "DELEGATE_ARGS": "-p x{prompt}"})


def test_delegate_timeout_range_validation():
    base = {
        "DISCORD_TOKEN": "tok-123",
        "OWNER_ID": "42",
        "DELEGATE_NAME": "helper",
        "DELEGATE_BIN": "/opt/helper",
        "DELEGATE_ARGS": "-p {prompt}",
    }
    # 5400 is not allowed (< 5400)
    with pytest.raises(ConfigError, match="DELEGATE_TIMEOUT_S"):
        load_settings({**base, "DELEGATE_TIMEOUT_S": "5400"})

    # 0 is not allowed (> 0)
    with pytest.raises(ConfigError, match="DELEGATE_TIMEOUT_S"):
        load_settings({**base, "DELEGATE_TIMEOUT_S": "0"})

    # negative
    with pytest.raises(ConfigError, match="DELEGATE_TIMEOUT_S"):
        load_settings({**base, "DELEGATE_TIMEOUT_S": "-10"})


def test_delegate_name_validation():
    base = {
        "DISCORD_TOKEN": "tok-123",
        "OWNER_ID": "42",
        "DELEGATE_BIN": "/opt/helper",
        "DELEGATE_ARGS": "-p {prompt}",
    }
    with pytest.raises(ConfigError, match="DELEGATE_NAME"):
        load_settings({**base, "DELEGATE_NAME": "rm -rf"})

    with pytest.raises(ConfigError, match="DELEGATE_NAME"):
        load_settings({**base, "DELEGATE_NAME": "-leadingdash"})


