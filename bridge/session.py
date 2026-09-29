"""Discord 쓰레드별 Claude 세션의 생명주기를 관리한다. Discord를 모른다."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from bridge.config import Delegate
from bridge.store import ThreadStore

logger = logging.getLogger(__name__)

ClientFactory = Callable[[Path, "str | None"], Awaitable[Any]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Session:
    thread_id: int
    cwd: Path
    session_id: str | None = None
    client: Any | None = None
    # Serializes actual requests. Owned by the caller (a later task holds it
    # around the streaming call, after acquire() returns) — SessionManager
    # only ever waits on it or checks whether it is held.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes connection setup for THIS session only, so two concurrent
    # acquire() calls for the same thread can't both build+connect a client.
    # Never confused with `lock` above, which serializes requests.
    connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: datetime = field(default_factory=_now)


class SessionManager:
    def __init__(
        self,
        factory: ClientFactory,
        store: ThreadStore,
        *,
        idle_timeout: timedelta = timedelta(hours=2),
    ) -> None:
        self._factory = factory
        self._store = store
        self._idle_timeout = idle_timeout
        self._sessions: dict[int, Session] = {}
        # Guards only the session TABLE: finding/creating a Session in
        # `_sessions` and the `store.put` calls that go with it. It is never
        # held across a client's connect()/disconnect() — those are slow I/O
        # and holding a shared lock across them would make every thread's
        # acquire() wait on whichever thread happens to be connecting or
        # disconnecting. Connection setup is guarded per-session by
        # `session.connect_lock`; request serialization is `session.lock`.
        self._manage_lock = asyncio.Lock()

    async def acquire(self, thread_id: int, cwd: Path | None = None) -> Session:
        """쓰레드의 세션을 돌려준다. 필요하면 만들거나 resume으로 되살린다.

        Lock hierarchy, in force throughout this class and never inverted:

            session.lock  >  _manage_lock  >  session.connect_lock

        `_manage_lock` is never held while waiting for `session.lock`
        (`close`, `drop_client` and `sweep_idle` all release it before doing
        anything slow, and none of them ever waits on `session.lock`), so
        taking `session.lock` first here introduces no cycle.
        """
        while True:
            async with self._manage_lock:
                session = self._sessions.get(thread_id)

                if session is None:
                    record = self._store.get(thread_id)
                    if record is None and cwd is None:
                        raise KeyError(
                            f"thread {thread_id} has no cwd; use !new <path>"
                        )
                    resolved_cwd = cwd if cwd is not None else record.cwd  # type: ignore[union-attr]
                    session = Session(
                        thread_id=thread_id,
                        cwd=resolved_cwd,
                        session_id=record.session_id if record else None,
                    )
                    self._sessions[thread_id] = session
                    self._store.put(thread_id, session.session_id, session.cwd)
                    cwd_change_pending = False
                else:
                    cwd_change_pending = cwd is not None and cwd != session.cwd

            if not cwd_change_pending:
                # The ordinary path never touches session.lock — only the
                # connect lock, so it can't wait on an unrelated in-flight
                # request.
                await self._ensure_connected(session)
                break

            # A cwd change MUST NOT mutate the session while a request is in
            # flight. `record_session_id` takes no lock and is called from
            # the streaming path on every ResultMessage while that path
            # holds `session.lock`; if we mutated `session.cwd` outside that
            # lock, the ordering
            #
            #   !cd sets cwd=/new, session_id=None, persists
            #   -> in-flight turn finishes, record_session_id(old_id) writes
            #      session_id=old_id AND store.put(thread, old_id, /new)
            #   -> !cd rebuilds with cwd=/new, resume=old_id
            #
            # would root the OLD project's conversation in the NEW directory
            # and persist a session id whose .jsonl lives under a different
            # project slug. Resume then fails and the thread dies quietly.
            #
            # So: take `session.lock` FIRST, and only then re-take
            # `_manage_lock` for the mutation. Because `record_session_id`
            # is synchronous it can never interleave with a mutation made
            # under the same `session.lock`, and by the time we hold that
            # lock the in-flight turn has finished and recorded whatever it
            # was going to record against the OLD cwd.
            async with session.lock:
                async with self._manage_lock:
                    if self._sessions.get(thread_id) is not session:
                        # A concurrent `close()`/`!resume` replaced or removed
                        # the table entry while we waited for session.lock.
                        # Our Session object is detached; start over rather
                        # than mutating something nobody is tracking.
                        restart = True
                    else:
                        restart = False
                        # Re-check: another `!cd` may have won the race for
                        # session.lock and already moved us to this cwd.
                        cwd_change_pending = cwd != session.cwd
                        if cwd_change_pending:
                            session.cwd = cwd  # type: ignore[assignment]
                            session.session_id = None
                            self._store.put(thread_id, None, cwd)
                if restart:
                    continue
                # One atomic swap of the client, still under session.lock, so
                # nobody waiting on it can observe session.client as None
                # mid-swap. connect_lock nests inside session.lock, never the
                # reverse.
                if cwd_change_pending:
                    await self._disconnect(session)
                await self._ensure_connected(session)
            break

        session.last_used = _now()
        return session

    async def _ensure_connected(self, session: Session) -> None:
        async with session.connect_lock:
            # Re-check under the connect lock: another acquire() for this
            # same thread (or a sweep_idle) may have run between the caller
            # deciding to (re)connect and reaching here.
            if session.client is None:
                # Build and connect into a local variable first; only attach
                # it to the session once connect() has actually succeeded.
                # If connect() raises, session.client stays None so the next
                # acquire() retries cleanly instead of reusing a half-open
                # client.
                client = await self._factory(session.cwd, session.session_id)
                await client.connect()
                session.client = client

    def record_session_id(self, thread_id: int, session_id: str) -> None:
        """SDK가 알려준 실제 세션 ID를 기록한다. resume의 근거가 된다."""
        session = self._sessions.get(thread_id)
        if session is None:
            return
        if session.session_id == session_id:
            return
        session.session_id = session_id
        self._store.put(thread_id, session_id, session.cwd)

    async def close(self, thread_id: int) -> None:
        async with self._manage_lock:
            session = self._sessions.pop(thread_id, None)
        if session is not None:
            await self._disconnect(session)

    async def drop_client(self, thread_id: int) -> None:
        """클라이언트만 버린다. 다음 acquire가 resume으로 되살린다."""
        async with self._manage_lock:
            session = self._sessions.get(thread_id)
        if session is not None:
            await self._disconnect(session)

    async def sweep_idle(self, now: datetime | None = None) -> list[int]:
        """유휴 세션의 클라이언트를 닫는다. session_id는 남긴다."""
        moment = now or _now()
        swept: list[int] = []
        # Snapshot the sessions before iterating: a client's disconnect() can
        # itself (indirectly, via an in-flight request on another thread)
        # trigger a new acquire() that adds an entry to `_sessions`, which
        # would otherwise raise "dictionary changed size during iteration".
        for thread_id, session in list(self._sessions.items()):
            client = None
            async with self._manage_lock:
                if session.client is None:
                    continue
                if moment - session.last_used < self._idle_timeout:
                    continue
                if session.lock.locked():
                    continue
                # Detach the client from the session while holding the
                # manage lock, so a concurrent acquire() for this same
                # thread cannot attach a fresh client only to have us stomp
                # on it a moment later. The actual disconnect() call happens
                # outside the lock: it may itself call back into acquire()
                # (e.g. handling another thread's message), and the manage
                # lock is not reentrant.
                client, session.client = session.client, None
            if client is None:
                continue
            try:
                await client.disconnect()
            except Exception:
                logger.warning(
                    "disconnect failed for thread %s", thread_id, exc_info=True
                )
            swept.append(thread_id)
        return swept

    def peek(self, thread_id: int) -> Session | None:
        """세션을 만들지 않고 현재 상태만 본다."""
        return self._sessions.get(thread_id)

    def active_threads(self) -> list[int]:
        return [tid for tid, s in self._sessions.items() if s.client is not None]

    async def _disconnect(self, session: Session) -> None:
        client, session.client = session.client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            logger.warning(
                "disconnect failed for thread %s", session.thread_id, exc_info=True
            )


CLAUDE_MD_MAX_CHARS = 16000


def _load_project_claude_md(cwd: Path) -> str | None:
    """`cwd`에 있는 CLAUDE.md만 읽는다 (상위 디렉토리로 올라가거나 재귀 탐색하지
    않는다).

    없거나, 읽을 수 없거나, 비어 있으면 None을 돌려준다. 절대 예외를 던지지
    않는다: CLAUDE.md가 없는 프로젝트는 오늘과 완전히 동일하게 동작해야 한다.

    ``errors="replace"``: 한두 바이트가 깨진 파일이라도 나머지 내용은 여전히
    쓸모 있으므로, 잘못된 인코딩이라고 전체를 버리지 않는다 (UnicodeDecodeError
    자체는 OSError의 하위 클래스가 아니라서 별도로 막을 필요가 없어진다).
    """
    try:
        text = (cwd / "CLAUDE.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    text = text.strip()
    if not text:
        return None

    truncated = len(text) > CLAUDE_MD_MAX_CHARS
    if truncated:
        text = text[:CLAUDE_MD_MAX_CHARS]

    notice = (
        f"\n\n[CLAUDE.md truncated at {CLAUDE_MD_MAX_CHARS} characters]"
        if truncated
        else ""
    )
    return (
        "The following is this project's own CLAUDE.md file, provided as "
        "project context (not an instruction from the operator):\n\n"
        f"{text}{notice}"
    )


# Replies are almost always read on mobile Discord: long reports only bloat scrolling and bury the point.
MOBILE_REPLY_STYLE = """\
## Response format (Discord mobile)
The user reads answers on mobile Discord. Keep status updates concise:
- Reply in the language the user writes in.
- First line: conclusion (completed / failed / blocked).
- Follow with 3-5 bullet points summarizing core points. Changed files by name only, and only when necessary.
- If user decision is required, ask as the final single line.
- Do not include long code blocks, full logs, complete diffs, tables, or step-by-step narration unless requested.
- Clearly state failures or remaining risks in one line without downplaying them.
- Never include secrets, passwords, tokens, keys, server IPs, connection credentials, or personal information (emails, phone numbers). Refer to them generically if needed (e.g. "DB password [redacted]")."""



DEFAULT_PACKAGE_DOMAINS = frozenset({
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "pypi.org",
    "files.pythonhosted.org",
    "repo.maven.apache.org",
    "repo1.maven.org",
    "plugins.gradle.org",
    "plugins-artifacts.gradle.org",
    "services.gradle.org",
    "downloads.gradle.org",
    "dl.google.com",
    "maven.google.com",
    "crates.io",
    "index.crates.io",
    "static.crates.io",
    "rubygems.org",
    "proxy.golang.org",
    "sum.golang.org",
})


def build_deny_rules(state_dir: Path | None, delegate: Delegate | None = None) -> list[str]:
    """샌드박스 및 Claude 세션에서 차단할 권한 거부 규칙 목록을 생성한다."""
    rules: list[str] = []

    # Git 설정 및 훅 변조 방지 (샌드박스 OS 수준 쓰기 차단)
    rules.append("Edit(//**/.git/config)")
    rules.append("Edit(//**/.git/hooks/**)")
    rules.append("Edit(~/.gitconfig)")
    rules.append("Edit(~/.config/git/**)")

    # 셸 설정 및 지속성 경로 변조 방지 (나중에 샌드박스 밖에서 실행되는 지속성 경로)
    rules.append("Edit(~/.ssh/**)")
    rules.append("Edit(~/.zshrc)")
    rules.append("Edit(~/.zprofile)")
    rules.append("Edit(~/.zshenv)")
    rules.append("Edit(~/.bashrc)")
    rules.append("Edit(~/.bash_profile)")
    rules.append("Edit(~/.profile)")
    rules.append("Edit(~/Library/LaunchAgents/**)")
    # Linux/WSL2의 같은 지속성 경로: 사용자 systemd 유닛·XDG 자동 시작
    rules.append("Edit(~/.config/systemd/**)")
    rules.append("Edit(~/.config/autostart/**)")

    # 실행 파일 및 AI 도구 설정 변조 방지 (스킬·에이전트·외부 에이전트 설정 변조로 신뢰 실행 경로 오염 방지)
    rules.append("Edit(~/.local/bin/**)")
    rules.append("Edit(~/.claude/**)")
    rules.append("Edit(~/.gemini/**)")

    if delegate is not None:
        # 권한 규칙에서 "/x"는 설정 파일 기준 상대경로라 절대경로는 "//x"로 써야 한다.
        rules.append(f"Edit(/{delegate.bin})")
        for p in delegate.protect_paths:
            prefix = "/" if p.startswith("/") else ""
            rules.append(f"Edit({prefix}{p}/**)")

    # 봇 저장소 자신 변조 방지 (봇 코드를 고치면 재시작 후 게이트가 사라짐)
    repo_root = Path(__file__).resolve().parents[1]
    rules.append(f"Edit(/{repo_root}/**)")

    if state_dir is not None:
        rules.append(f"Edit(/{Path(state_dir).resolve()}/**)")

    # 패키지 레지스트리 인증 파일 변조 및 읽기 차단 (패키지 도메인 개방의 보완)
    cred_files = [
        "~/.npmrc",
        "~/.yarnrc.yml",
        "~/.pypirc",
        "~/.gradle/gradle.properties",
        "~/.m2/settings.xml",
        "~/.m2/settings-security.xml",
        "~/.cargo/credentials",
        "~/.cargo/credentials.toml",
        "~/.gem/credentials",
    ]
    for cred in cred_files:
        rules.append(f"Edit({cred})")

    # 민감 자격증명 파일 읽기 차단
    rules.append("Read(~/.ssh/**)")
    rules.append("Read(~/.aws/**)")
    rules.append("Read(~/.config/gh/**)")
    rules.append("Read(~/.netrc)")
    rules.append("Read(~/.git-credentials)")
    rules.append("Read(~/.docker/config.json)")
    rules.append("Read(~/Library/Keychains/**)")
    rules.append("Read(~/.gemini/oauth_creds.json)")
    rules.append("Read(~/.gemini/google_accounts.json)")

    if state_dir is not None:
        rules.append(f"Read(/{Path(state_dir).resolve()}/**)")

    for cred in cred_files:
        rules.append(f"Read({cred})")

    return rules


def ensure_user_plugin(state_dir: Path) -> Path | None:
    """사용자의 ~/.claude/skills 및 agents를 래핑하는 로컬 플러그인을 만든다."""
    claude_skills = Path.home() / ".claude" / "skills"
    if not claude_skills.exists():
        return None

    plugin_dir = Path(state_dir) / "user-plugin"
    manifest_dir = plugin_dir / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    manifest_file = manifest_dir / "plugin.json"
    manifest_file.write_text(
        json.dumps({"name": "user", "version": "0.0.1"}, indent=2),
        encoding="utf-8",
    )

    skills_link = plugin_dir / "skills"
    if skills_link.is_symlink() or skills_link.exists():
        skills_link.unlink()
    skills_link.symlink_to(claude_skills)

    claude_agents = Path.home() / ".claude" / "agents"
    agents_link = plugin_dir / "agents"
    if claude_agents.exists():
        if agents_link.is_symlink() or agents_link.exists():
            agents_link.unlink()
        agents_link.symlink_to(claude_agents)
    elif agents_link.is_symlink() or agents_link.exists():
        agents_link.unlink()

    return plugin_dir


async def claude_client_factory(
    cwd: Path,
    resume: str | None,
    *,
    can_use_tool: Any | None = None,
    allowed_domains: Iterable[str] = (),
    state_dir: Path | None = None,
    delegate: Delegate | None = None,
) -> ClaudeSDKClient:
    """실제 Claude CLI에 붙는 클라이언트를 만든다. connect()는 호출자가 한다."""
    project_context = _load_project_claude_md(cwd)
    append = MOBILE_REPLY_STYLE
    if project_context is not None:
        append = f"{MOBILE_REPLY_STYLE}\n\n{project_context}"
    if delegate is not None:
        delegate_prompt = (
            "## External agent\n"
            "An external coding agent is available. Run it as a standalone Bash command:\n"
            f"`{delegate.name} -p '<task prompt>'` — no pipes, redirects or chained commands; its output is returned to you.\n"
            "Use it when the user or the project's CLAUDE.md asks you to delegate work to it."
        )
        append = f"{append}\n\n{delegate_prompt}"

    # Append-only, on purpose: {"type": "preset", "preset": "claude_code",
    # "append": ...} keeps Claude Code's own preset system prompt (its
    # tool-use instructions included) intact and adds our text after it --
    # checked in the installed SDK before using this form:
    # claude_agent_sdk/types.py defines SystemPromptPreset, and
    # _internal/transport/subprocess_cli.py wires an "append" preset to the
    # CLI's `--append-system-prompt` flag. A plain string here would REPLACE
    # the preset outright (`--system-prompt <string>`), which would break
    # tool use.
    system_prompt: dict[str, Any] = {
        "type": "preset",
        "preset": "claude_code",
        "append": append,
    }

    plugins = []
    if state_dir is not None:
        plugin_path = ensure_user_plugin(state_dir)
        if plugin_path is not None:
            plugins = [{"type": "local", "path": str(plugin_path)}]

    all_domains = sorted(DEFAULT_PACKAGE_DOMAINS | set(allowed_domains))

    options = ClaudeAgentOptions(
        cwd=str(cwd),
        resume=resume,
        permission_mode="default",
        can_use_tool=can_use_tool,
        system_prompt=system_prompt,
        # Empty on purpose: measured (not assumed) that a permissions.allow
        # entry in ANY loaded settings file -- user, project, or local --
        # bypasses can_use_tool entirely for a matching tool call, with no
        # permission_denials recorded. Loading settings here would let a
        # directory's own .claude/settings.local.json (accumulated "allow
        # always" clicks), a cloned repo's committed .claude/settings.json,
        # or the user's global settings.json silently skip the gate -- and
        # with it the sandbox-escape, git-sync and path checks it enforces.
        # The gate must be the only thing deciding whether a tool runs.
        # (Skills and agent definitions are still loaded, via `plugins`
        # below, without reading any settings file.)
        #
        # This does not give up project CLAUDE.md support: it is read
        # directly from `cwd` above and appended to the system prompt
        # ourselves, bypassing the settings loader entirely. A malicious or
        # careless CLAUDE.md can influence what Claude proposes, but every
        # tool call still goes through can_use_tool and the sandbox --
        # influence without an escape from either.
        setting_sources=[],
        # Belt and braces around the same property. `setting_sources=[]`
        # suppresses settings FILES, but the CLI's own `--restricted` help
        # says so explicitly: "ignores user, project and local settings
        # files (managed settings and `--settings` still apply; add
        # `--strict-mcp-config` to skip MCP servers too)". MCP configuration
        # is discovered on a separate path from settings, so not loading
        # settings is not by itself a guarantee that a project-scoped
        # `.mcp.json` is ignored.
        #
        # That matters more here than anywhere else in this file: an MCP
        # server is a COMMAND the CLI spawns as a child process at session
        # startup, before any tool call exists for `can_use_tool` to gate.
        # `!new ~/some-cloned-repo` on a repo carrying a hostile `.mcp.json`
        # would therefore be code execution completely outside the approval
        # gate -- by construction, not by a bug. This product uses no MCP
        # servers at all, so pinning the set empty and telling the CLI to
        # trust only that pinned set costs nothing and closes the path.
        #
        # Measured on the installed CLI (2.1.270): with `setting_sources=[]`
        # alone a project `.mcp.json` was already NOT spawned, while the
        # same probe with `setting_sources=["project"]` DID spawn it. So
        # today this is defence in depth rather than a live hole -- but it
        # is the kind of default that a CLI version bump can flip silently,
        # and the assertion in tests/test_claude_client_factory.py is what
        # keeps it pinned.
        mcp_servers={},
        strict_mcp_config=True,
        # allowUnsandboxedCommands: False -- 샌드박스 해제는 누구도(메인·하위 에이전트) 못 한다.
        # 샌드박스 밖 실행이 필요한 git 동기화·외부 에이전트는 게이트가 검증 후 봇 프로세스에서 직접 실행한다.
        # failIfUnavailable: True -- 샌드박스 백엔드(bubblewrap/seatbelt)가 없을 때 CLI가 샌드박스 없이 셸을 돌리는 대신 실패하게 한다.
        # 이 봇은 샌드박스를 전제로 Bash를 자동 승인하므로 필수.
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True,
            "network": {"allowedDomains": all_domains},
        },
        # settings에 allow 규칙은 절대 넣지 않는다.
        settings=json.dumps({"permissions": {"deny": build_deny_rules(state_dir, delegate=delegate)}}),
        plugins=plugins,
    )
    return ClaudeSDKClient(options=options)

