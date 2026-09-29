"""can_use_tool 게이트. 위험한 도구는 Discord 승인을 거친다."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from bridge.config import Delegate
from bridge.render import summarize_tool_use

logger = logging.getLogger(__name__)

_SHELL_PUNCT = set(";&|<>()")

_GIT_PUSH_OPTS = frozenset({
    "-u", "--set-upstream", "--tags", "--follow-tags", "-n", "--dry-run",
    "-v", "--verbose", "-q", "--quiet", "--no-verify", "--force-with-lease",
})
_GIT_FETCH_OPTS = frozenset({
    "--all", "--prune", "-p", "--tags", "-n", "--dry-run",
    "-v", "--verbose", "-q", "--quiet",
})
_GIT_PULL_OPTS = frozenset({
    "--rebase", "-r", "--no-rebase", "--ff-only", "--no-edit",
    "-v", "--verbose", "-q", "--quiet",
})
_REFSPEC_PATTERN = re.compile(r"^\+?[A-Za-z0-9._/@^:-]+$")


def _has_unquoted_newline(command: str) -> bool:
    in_single = False
    in_double = False
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "\n" and not in_single and not in_double:
            return True
    return False


def _tokens(command: str) -> list[str] | None:
    """셸 연산자가 따옴표 밖에 있으면 None. 아니면 posix shlex 토큰."""
    if _has_unquoted_newline(command):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None

    if not tokens:
        return None

    for t in tokens:
        if set(t).issubset(_SHELL_PUNCT):
            return None

    return tokens


def parse_git_sync(command: str, cwd: Path | None) -> list[str] | None:
    """검증 통과한 git push/fetch/pull 명령을 argv로 반환. 아니면 None."""
    if cwd is None:
        return None
    tokens = _tokens(command)
    if not tokens or len(tokens) < 2:
        return None
    if tokens[0] != "git":
        return None
    sub = tokens[1]
    if sub not in {"push", "fetch", "pull"}:
        return None

    rest = tokens[2:]
    pos_args: list[str] = []

    for t in rest:
        if t.startswith("-"):
            if sub == "push":
                if t not in _GIT_PUSH_OPTS and not t.startswith("--force-with-lease="):
                    return None
            elif sub == "fetch":
                if t not in _GIT_FETCH_OPTS:
                    return None
            elif sub == "pull":
                if t not in _GIT_PULL_OPTS:
                    return None
        else:
            pos_args.append(t)

    if pos_args:
        # 첫 번째 위치 인자는 remote 이름
        try:
            proc = subprocess.run(
                ["git", "-C", str(cwd), "remote"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode != 0:
                return None
            remotes = set(proc.stdout.splitlines())
        except (subprocess.SubprocessError, OSError):
            return None

        if pos_args[0] not in remotes:
            return None

        # 나머지는 refspec
        for ref in pos_args[1:]:
            if not _REFSPEC_PATTERN.fullmatch(ref):
                return None
            if "://" in ref:
                return None
            if sub == "push" and ref.startswith("+"):
                return None

    no_verify = ["--no-verify"] if sub == "push" and "--no-verify" not in rest else []
    return [
        "git",
        "-C",
        str(cwd),
        "-c",
        "core.hooksPath=/dev/null",
        sub,
        *no_verify,
        *rest,
    ]


def parse_delegate(command: str, cwd: Path | None, delegate: Delegate | None) -> list[str] | None:
    """허용 형태: `<name> -p <prompt>` 또는 `<name> --prompt <prompt>` 딱 이것뿐.
    토큰이 정확히 3개가 아니면 None. 모델이 플래그를 추가할 수 없다 —
    플래그는 운영자가 DELEGATE_ARGS로만 정한다.
    반환: [str(delegate.bin), *args의 "{prompt}"를 prompt로 치환한 것]"""
    if delegate is None or cwd is None:
        return None
    tokens = _tokens(command)
    if not tokens or len(tokens) != 3:
        return None

    first = tokens[0]
    if first != delegate.name and first != str(delegate.bin):
        return None

    flag = tokens[1]
    if flag not in {"-p", "--prompt"}:
        return None

    prompt = tokens[2]
    # "-"로 시작하는 프롬프트는 {prompt} 위치에서 에이전트 플래그로 해석될 수 있다
    # (예: codex exec --dangerously-bypass-approvals-and-sandbox). 모델의 플래그 주입 차단.
    if not prompt.strip() or prompt.startswith("-"):
        return None
    substituted = [prompt if t == "{prompt}" else t for t in delegate.args]
    return [str(delegate.bin), *substituted]


def looks_like_git_sync(command: str) -> bool:
    return bool(re.search(r"\bgit\b", command) and re.search(r"\b(push|fetch|pull)\b", command))


def looks_like_delegate(command: str, delegate: Delegate | None) -> bool:
    """delegate가 None이면 항상 False. 아니면 name을 경계 매칭."""
    if delegate is None:
        return False
    escaped = re.escape(delegate.name)
    return bool(re.search(rf"(^|[\s/;&|(]){escaped}(\s|$)", command))


# 자동 허용: 읽기 전용이거나 부작용이 로컬에 머무는 도구. WebFetch/WebSearch는
# 네트워크 egress를 일으키고 어디로 나갈지가 Claude가 읽은 내용에 따라 바뀔 수
# 있으므로 (SSRF/유출 경로) BLOCKED에 있다. 목록 어디에도 없는 도구는 승인을 거친다.
# Agent/Task는 하위 에이전트의 Bash도 게이트·샌드박스를 거침이 실측됨 (실측 12).
AUTO_ALLOW = frozenset({
    "Read", "Grep", "Glob", "TodoWrite", "AskUserQuestion",
    "BashOutput", "KillShell", "KillBash", "Agent", "Task", "Skill",
})

BLOCKED = frozenset({"WebFetch", "WebSearch", "SandboxNetworkAccess"})

PATH_TOOLS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

RESULT_TAIL_CHARS = 30000

_PUBLISH_PATTERN = re.compile(
    r"\b(npm|pnpm|bun)\s+(un)?publish\b|\byarn\s+(npm\s+)?publish\b"
    r"|\b(twine\s+upload|(poetry|uv|flit|hatch)\s+publish|cargo\s+publish|gem\s+push|(docker|podman)\s+push)\b"
    r"|\bmvnw?\b[^\n]*\bdeploy\b|\bgradlew?\b[^\n]*\bpublish\w*",
    re.IGNORECASE,
)


def is_publish_command(command: str) -> bool:
    """패키지 레지스트리 배포 명령인지 검사 (1차 필터)."""
    return bool(_PUBLISH_PATTERN.search(command))


async def run_trusted(argv: list[str], cwd: Path, timeout_s: float) -> tuple[str, int]:
    """샌드박스 밖에서 argv를 셸 없이 실행하고 (결과 파일 경로, 종료 코드)를 돌려준다."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        output = f"[실행 실패] {exc}"
        rc = 127
    else:
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            rc = proc.returncode if proc.returncode is not None else 0
            output = stdout_bytes.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                stdout_bytes, _ = await proc.communicate()
                output = stdout_bytes.decode("utf-8", errors="replace")
            except Exception:
                output = ""
            output += f"\n[⏱ {timeout_s}s 초과로 중단]"
            rc = 124

    if len(output) > RESULT_TAIL_CHARS:
        output = "[…앞부분 생략…]\n" + output[-RESULT_TAIL_CHARS:]

    temp_dir = Path(tempfile.mkdtemp(prefix="bridge-run-"))
    out_file = temp_dir / "out.txt"
    out_file.write_text(output, encoding="utf-8")
    return str(out_file), rc


Decision = Literal["allow", "allow_all", "deny"]
Prompter = Callable[[str, dict[str, Any], str], Awaitable[Decision]]

_DENY_TIMEOUT = "⏱ 승인 시간 초과로 거부되었습니다."
_DENY_USER = "사용자가 거부했습니다."
_DENY_ERROR = "승인 요청을 전달하지 못해 거부했습니다."


class ApprovalGate:
    """세션 하나당 인스턴스 하나. allow_all은 이 인스턴스 수명 동안만 유효하다."""

    def __init__(
        self,
        prompter: Prompter,
        audit_path: Path,
        *,
        timeout_s: float = 120.0,
        on_block: Callable[[str, str], Awaitable[None]] | None = None,
        delegate: Delegate | None = None,
        git_timeout_s: float = 300.0,
    ) -> None:
        self._prompter = prompter
        self._audit_path = audit_path
        self._timeout_s = timeout_s
        self._on_block = on_block
        self.delegate = delegate
        self.git_timeout_s = git_timeout_s
        self.cwd: Path | None = None
        self._blanket: set[str] = set()

    async def _block(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        reason: str,
    ) -> PermissionResultDeny:
        audit_input = {**tool_input, "reason": reason}
        self._audit(tool_name, audit_input, "blocked")
        if self._on_block is not None:
            try:
                summary = summarize_tool_use(tool_name, tool_input)
                await self._on_block(summary, reason)
            except Exception:
                logger.exception("on_block callback failed for %s", tool_name)
        return PermissionResultDeny(message=reason, interrupt=False)

    async def _trusted(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        argv: list[str],
        timeout: float,
    ) -> PermissionResultAllow:
        assert self.cwd is not None
        out, rc = await run_trusted(argv, self.cwd, timeout)
        audit_input = {**tool_input, "trusted_argv": str(argv), "rc": str(rc)}
        self._audit(tool_name, audit_input, "trusted_run")
        updated_input = dict(tool_input)
        updated_input.pop("dangerouslyDisableSandbox", None)
        updated_input["command"] = f"cat {shlex.quote(out)}; exit {rc}"
        return PermissionResultAllow(updated_input=updated_input)

    async def __call__(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> PermissionResultAllow | PermissionResultDeny:
        # 1. AUTO_ALLOW
        if tool_name in AUTO_ALLOW:
            self._audit(tool_name, tool_input, "auto_allow")
            return PermissionResultAllow()

        # 2. BLOCKED 또는 mcp__*
        if tool_name in BLOCKED or tool_name.startswith("mcp__"):
            if tool_name == "SandboxNetworkAccess":
                reason = f"외부 접속 차단: {tool_input.get('host')}"
            else:
                reason = f"{tool_name} 사용 차단"
            return await self._block(tool_name, tool_input, reason)

        # 3. Bash
        if tool_name == "Bash":
            cmd = str(tool_input.get("command", ""))
            if bool(tool_input.get("dangerouslyDisableSandbox")):
                return await self._block(tool_name, tool_input, "샌드박스 해제 요청 차단")

            git_argv = parse_git_sync(cmd, self.cwd)
            if git_argv is not None:
                return await self._trusted(tool_name, tool_input, git_argv, self.git_timeout_s)
            if looks_like_git_sync(cmd):
                return await self._block(
                    tool_name,
                    tool_input,
                    "git push/fetch/pull은 단독 명령으로, 기존 remote 이름만: git push [옵션] <remote> [refspec]",
                )

            del_argv = parse_delegate(cmd, self.cwd, self.delegate)
            if del_argv is not None:
                assert self.delegate is not None
                return await self._trusted(tool_name, tool_input, del_argv, self.delegate.timeout_s)
            if looks_like_delegate(cmd, self.delegate):
                assert self.delegate is not None
                name = self.delegate.name
                return await self._block(
                    tool_name,
                    tool_input,
                    f"{name}는 단독 명령으로만: {name} -p '<프롬프트>' (파이프·리다이렉트·다른 명령 연결 불가, 결과는 그대로 돌려줌)",
                )

            if is_publish_command(cmd):
                return await self._block(tool_name, tool_input, "패키지 배포 명령 차단")

            self._audit(tool_name, tool_input, "auto_allow_sandboxed")
            return PermissionResultAllow()

        # 4. PATH_TOOLS
        if tool_name in PATH_TOOLS:
            path_key = PATH_TOOLS[tool_name]
            raw_path = str(tool_input.get(path_key, ""))
            if self.cwd is None:
                return await self._block(tool_name, tool_input, f"작업 디렉터리 밖 쓰기 차단: {raw_path}")
            try:
                target = Path(raw_path).expanduser()
                if not target.is_absolute():
                    target = self.cwd / target
                resolved_target = target.resolve()
                resolved_cwd = self.cwd.resolve()
                if resolved_target != resolved_cwd and not resolved_target.is_relative_to(resolved_cwd):
                    return await self._block(tool_name, tool_input, f"작업 디렉터리 밖 쓰기 차단: {raw_path}")
            except Exception:
                return await self._block(tool_name, tool_input, f"작업 디렉터리 밖 쓰기 차단: {raw_path}")

            self._audit(tool_name, tool_input, "auto_allow")
            return PermissionResultAllow()

        # 5. 그 외 모르는 도구 (기존 승인 로직)
        if tool_name in self._blanket:
            self._audit(tool_name, tool_input, "allow_all_cached")
            return PermissionResultAllow()

        try:
            summary = summarize_tool_use(tool_name, tool_input)
            decision = await asyncio.wait_for(
                self._prompter(tool_name, tool_input, summary),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            self._audit(tool_name, tool_input, "timeout")
            return PermissionResultDeny(message=_DENY_TIMEOUT, interrupt=False)
        except Exception:
            logger.exception("approval prompt failed for %s", tool_name)
            self._audit(tool_name, tool_input, "error")
            return PermissionResultDeny(message=_DENY_ERROR, interrupt=False)

        self._audit(tool_name, tool_input, decision)

        if decision == "allow_all":
            self._blanket.add(tool_name)
            return PermissionResultAllow()
        if decision == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message=_DENY_USER, interrupt=False)

    def _audit(self, tool_name: str, tool_input: dict[str, Any], decision: str) -> None:
        # 감사 로그 기록은 절대 결정 자체를 깨서는 안 된다: OSError(디스크 문제)든
        # 직렬화 불가능한 값이든, 여기서 나는 예외는 전부 삼키고 경고만 남긴다.
        try:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "tool": tool_name,
                # 2000, not 200: this file is the forensic record of what
                # actually ran on this machine. A realistic Bash command --
                # a long pipeline, a heredoc, a find with many predicates --
                # clips well past 200 characters, and the clipped tail is
                # exactly the part worth reading after the fact. 2000 keeps
                # a full command intact while still bounding a pathological
                # input (a whole file's contents in a Write).
                "input": {k: str(v)[:2000] for k, v in tool_input.items()},
                "decision": decision,
            }
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.warning("could not write audit log", exc_info=True)

