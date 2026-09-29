"""환경변수에서 봇 설정을 읽고 검증한다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


import re
import shlex


class ConfigError(Exception):
    """필수 설정이 없거나 형식이 잘못됐다."""


_DELEGATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Delegate:
    name: str               # Claude가 치는 명령 단어. 예: "codex", "gemini"
    bin: Path               # 실행 파일 절대경로
    args: tuple[str, ...]   # 인자 템플릿. 정확히 한 토큰이 "{prompt}"
    timeout_s: float
    protect_paths: tuple[str, ...]  # Edit deny로 보호할 에이전트 설정 경로 (~ 허용)


@dataclass(frozen=True)
class Settings:
    token: str
    owner_id: int
    guild_ids: frozenset[int]
    channel_ids: frozenset[int]
    state_dir: Path
    default_cwd: Path
    idle_timeout_hours: float
    approval_timeout_s: float
    sandbox_allowed_domains: frozenset[str] = frozenset()
    delegate: Delegate | None = None


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required; refusing to start")
    return value


def _int(value: str, key: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a numeric Discord snowflake") from exc
    # A Discord snowflake is always positive. `OWNER_ID=0` parsed cleanly
    # before this check and produced a bot that compared every author id
    # against 0 -- a boot guard the spec calls mandatory, silently disabled
    # by a typo or an unset variable that expanded to an empty-looking "0".
    # The one rail this product cannot afford to have quietly fail open.
    if parsed <= 0:
        raise ConfigError(f"{key} must be a positive Discord snowflake, got {parsed}")
    return parsed


def _parse_delegate(env: Mapping[str, str]) -> Delegate | None:
    raw_name = env.get("DELEGATE_NAME", "").strip()
    if not raw_name:
        return None

    if not _DELEGATE_NAME_RE.match(raw_name):
        raise ConfigError(
            f"DELEGATE_NAME must match ^[A-Za-z0-9][A-Za-z0-9._-]*$, got {raw_name!r}"
        )

    raw_bin = env.get("DELEGATE_BIN", "").strip()
    if not raw_bin:
        raise ConfigError("DELEGATE_BIN is required when DELEGATE_NAME is set")
    bin_path = Path(raw_bin).expanduser()
    if not bin_path.is_absolute():
        raise ConfigError(f"DELEGATE_BIN must be an absolute path, got {raw_bin!r}")

    raw_args = env.get("DELEGATE_ARGS", "").strip()
    if not raw_args:
        raise ConfigError("DELEGATE_ARGS is required when DELEGATE_NAME is set")
    tokens = tuple(shlex.split(raw_args))
    prompt_tokens = [t for t in tokens if t == "{prompt}"]
    partial_prompts = [t for t in tokens if "{prompt}" in t and t != "{prompt}"]
    if len(prompt_tokens) != 1 or len(partial_prompts) > 0:
        raise ConfigError(
            "DELEGATE_ARGS must contain exactly one '{prompt}' token, with no partial substrings"
        )

    raw_timeout = env.get("DELEGATE_TIMEOUT_S", "3000").strip()
    try:
        timeout_s = float(raw_timeout)
    except ValueError as exc:
        raise ConfigError(f"DELEGATE_TIMEOUT_S must be a number, got {raw_timeout!r}") from exc
    if not (0 < timeout_s < 5400):
        raise ConfigError(
            f"DELEGATE_TIMEOUT_S must be > 0 and < 5400, got {timeout_s}"
        )

    raw_protect = env.get("DELEGATE_PROTECT_PATHS", "").strip()
    protect_paths = tuple(part.strip() for part in raw_protect.split(",") if part.strip())

    return Delegate(
        name=raw_name,
        bin=bin_path,
        args=tokens,
        timeout_s=timeout_s,
        protect_paths=protect_paths,
    )


def load_settings(env: Mapping[str, str]) -> Settings:
    token = _required(env, "DISCORD_TOKEN")
    owner_id = _int(_required(env, "OWNER_ID"), "OWNER_ID")

    raw_guilds = env.get("GUILD_IDS", "").strip()
    guild_ids = frozenset(
        _int(part.strip(), "GUILD_IDS")
        for part in raw_guilds.split(",")
        if part.strip()
    )

    raw_channels = env.get("CHANNEL_IDS", "").strip()
    channel_ids = frozenset(
        _int(part.strip(), "CHANNEL_IDS")
        for part in raw_channels.split(",")
        if part.strip()
    )

    state_dir = Path(env.get("STATE_DIR", "~/.claude-discord")).expanduser()
    default_cwd = Path(env.get("DEFAULT_CWD", "~")).expanduser()

    raw_sandbox_domains = env.get("SANDBOX_ALLOWED_DOMAINS", "").strip()
    sandbox_allowed_domains = frozenset(
        part.strip().lower()
        for part in raw_sandbox_domains.split(",")
        if part.strip()
    )

    delegate = _parse_delegate(env)

    return Settings(
        token=token,
        owner_id=owner_id,
        guild_ids=guild_ids,
        channel_ids=channel_ids,
        state_dir=state_dir,
        default_cwd=default_cwd,
        idle_timeout_hours=float(env.get("IDLE_TIMEOUT_HOURS", "2")),
        approval_timeout_s=float(env.get("APPROVAL_TIMEOUT_S", "120")),
        sandbox_allowed_domains=sandbox_allowed_domains,
        delegate=delegate,
    )

