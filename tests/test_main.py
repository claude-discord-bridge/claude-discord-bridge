"""bridge/__main__.py의 부팅 전 점검 로직을 검증한다.

전체 부팅 거부(설정 오류 -> exit 2)는 태스크 8 검증 절차의 셸 명령으로 이미
확인한다 (`env -i ... python -m bridge`). 여기서는 그 아래에서 실제로
`BridgeBot.run()`을 호출하지 않고도 단위 테스트로 증명 가능한 두 가지만
다룬다: GUILD_IDS가 비었을 때의 경고, `.env` 파일 권한 경고.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path

import pytest
from bridge.__main__ import _check_guild_ids, _load_env_file
from bridge.config import Settings


def make_settings(tmp_path: Path, guild_ids: frozenset[int]) -> Settings:
    return Settings(
        token="t",
        owner_id=1,
        guild_ids=guild_ids,
        channel_ids=frozenset(),
        state_dir=tmp_path / "state",
        default_cwd=tmp_path,
        idle_timeout_hours=2.0,
        approval_timeout_s=120.0,
    )


def test_empty_guild_ids_logs_a_visible_warning(tmp_path, caplog):
    settings = make_settings(tmp_path, frozenset())
    with caplog.at_level(logging.WARNING, logger="bridge.__main__"):
        _check_guild_ids(settings)
    assert any("GUILD_IDS is empty" in r.message for r in caplog.records)


def test_nonempty_guild_ids_logs_nothing(tmp_path, caplog):
    settings = make_settings(tmp_path, frozenset({123}))
    with caplog.at_level(logging.WARNING, logger="bridge.__main__"):
        _check_guild_ids(settings)
    assert caplog.records == []


def test_loose_env_file_permissions_warn(tmp_path, caplog):
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=abc\n")
    env_path.chmod(0o644)  # group/other readable

    with caplog.at_level(logging.WARNING, logger="bridge.__main__"):
        _load_env_file(env_path)

    assert any("readable by group or other" in r.message for r in caplog.records)


def test_locked_down_env_file_permissions_are_silent(tmp_path, caplog):
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=abc\n")
    env_path.chmod(0o600)

    with caplog.at_level(logging.WARNING, logger="bridge.__main__"):
        _load_env_file(env_path)

    assert caplog.records == []


def test_load_env_file_still_sets_environ(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("SOME_TEST_KEY=value\n")
    env_path.chmod(0o600)
    monkeypatch.delenv("SOME_TEST_KEY", raising=False)

    _load_env_file(env_path)

    import os

    assert os.environ["SOME_TEST_KEY"] == "value"


def test_trailing_comment_is_stripped_from_env_values(tmp_path, monkeypatch):
    """`KEY=value  # 설명` 의 주석이 값에 섞여 들어가면 봇이 뜨지 않는다."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "CHANNEL_IDS=123  # 이 맥북\n"
        "OWNER_ID=7 # 나\n"
        "PASSPHRASE=a#b\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CHANNEL_IDS", raising=False)
    monkeypatch.delenv("OWNER_ID", raising=False)
    monkeypatch.delenv("PASSPHRASE", raising=False)

    _load_env_file(env_path)

    assert os.environ["CHANNEL_IDS"] == "123"
    assert os.environ["OWNER_ID"] == "7"
    # 공백 없는 `#`는 값의 일부다 — 암호를 조용히 잘라내면 안 된다.
    assert os.environ["PASSPHRASE"] == "a#b"


def test_main_rejects_win32(monkeypatch, capsys):
    import sys
    from bridge.__main__ import main

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Native Windows is not supported" in captured.err
    assert "WSL2" in captured.err


def test_main_mode_claude_only(monkeypatch, caplog):
    import sys
    from bridge.__main__ import main

    monkeypatch.setattr(sys, "platform", "darwin")
    fake_env = {
        "DISCORD_TOKEN": "tok",
        "OWNER_ID": "42",
        "GUILD_IDS": "1",
    }
    monkeypatch.setattr(os, "environ", fake_env)
    monkeypatch.setattr("bridge.__main__._load_env_file", lambda _: None)
    bot_ran = []

    class DummyBot:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *args, **kwargs):
            bot_ran.append(self.settings)

    monkeypatch.setattr("bridge.__main__.BridgeBot", DummyBot)

    with caplog.at_level(logging.INFO, logger="bridge.__main__"):
        main()
    assert any("mode: claude-only" in r.message for r in caplog.records)
    assert len(bot_ran) == 1


def test_main_mode_delegate_and_binary_warning(monkeypatch, caplog, tmp_path):
    import sys
    from bridge.__main__ import main

    monkeypatch.setattr(sys, "platform", "darwin")
    non_exec = tmp_path / "fake_agent"
    non_exec.write_text("dummy")
    non_exec.chmod(0o644)

    fake_env = {
        "DISCORD_TOKEN": "tok",
        "OWNER_ID": "42",
        "GUILD_IDS": "1",
        "DELEGATE_NAME": "helper",
        "DELEGATE_BIN": str(non_exec),
        "DELEGATE_ARGS": "-p {prompt}",
    }
    monkeypatch.setattr(os, "environ", fake_env)
    monkeypatch.setattr("bridge.__main__._load_env_file", lambda _: None)
    bot_ran = []

    class DummyBot:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *args, **kwargs):
            bot_ran.append(self.settings)

    monkeypatch.setattr("bridge.__main__.BridgeBot", DummyBot)

    with caplog.at_level(logging.INFO, logger="bridge.__main__"):
        main()
    assert any("not executable" in r.message for r in caplog.records)
    assert any("mode: claude + delegate 'helper'" in r.message for r in caplog.records)
    assert len(bot_ran) == 1

