"""python -m bridge 로 봇을 띄운다."""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

from bridge.bot import BridgeBot
from bridge.config import ConfigError, Settings, load_settings

logger = logging.getLogger(__name__)


def _load_env_file(path: Path) -> None:
    """~/.claude-discord/.env 를 os.environ 에 얹는다. 이미 있는 값은 덮지 않는다."""
    if not path.exists():
        return

    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            # 거부하지는 않는다: 이 파일이 있어야 봇 토큰을 읽을 수 있으므로
            # 여기서 막으면 그냥 부팅이 안 되는 것과 다를 바 없다. 대신 봇
            # 토큰이 든 파일이 그룹/전체에 읽히고 있다는 것만 크게 경고한다.
            logger.warning(
                "%s is readable by group or other (mode %o); it holds the "
                "Discord bot token and should be chmod 600",
                path,
                stat.S_IMODE(mode),
            )
    except OSError:
        logger.warning("could not stat %s to check its permissions", path)

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # ` # ...` 꼬리 주석을 잘라낸다. 문서가 `CHANNEL_IDS=123  # 이 맥북`
        # 처럼 보여주는데, 이게 값으로 들어가면 봇은 뜨지 않고 "must be a
        # numeric Discord snowflake" 한 줄만 남기고 죽는다. 값 안에 그냥 있는
        # `#`(암호 등)은 건드리지 않도록 앞에 공백이 있을 때만 자른다.
        head, sep, _ = value.partition(" #")
        if sep:
            value = head.rstrip()
        os.environ.setdefault(key.strip(), value.strip('"').strip("'"))


def _check_guild_ids(settings: Settings) -> None:
    """GUILD_IDS가 비어 있으면 길드 메시지는 전부 거부된다 (`_is_owner`).
    거부 자체는 맞는 동작이지만, 설정을 잊었을 뿐인 소유자에게는 로그에 아무
    설명도 없이 봇이 그냥 응답하지 않는 것처럼 보인다. 동작은 바꾸지 않고
    눈에 띄게만 만든다."""
    if not settings.guild_ids:
        logger.warning(
            "GUILD_IDS is empty: every message from a guild (server) channel "
            "will be rejected by the owner check, and DMs cannot host "
            "threads either (!new requires a text channel to create a "
            "thread on). The bot will not be usable until GUILD_IDS is set."
        )


def main() -> None:
    if sys.platform == "win32":
        print(
            "Native Windows is not supported: Claude Code's sandbox only runs on macOS, Linux and WSL2. "
            "Run this bot inside WSL2 — see README 'Windows (WSL2)'.",
            file=sys.stderr,
        )
        sys.exit(2)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _load_env_file(Path("~/.claude-discord/.env").expanduser())

    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if settings.delegate is not None:
        if not os.access(settings.delegate.bin, os.X_OK):
            logger.warning(
                "delegate binary %s is not executable or does not exist",
                settings.delegate.bin,
            )
        logger.info("mode: claude + delegate '%s'", settings.delegate.name)
    else:
        logger.info("mode: claude-only")

    _check_guild_ids(settings)

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    BridgeBot(settings).run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
