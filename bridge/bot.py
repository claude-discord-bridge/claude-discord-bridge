"""Discord 진입점. 이벤트 수신, 소유자 인가, 나머지 모듈 조립."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import io
import logging
import time
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any

import discord
from claude_agent_sdk import (
    AssistantMessage,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from bridge.config import Settings
from bridge.permissions import ApprovalGate, Decision
from bridge.render import (
    ATTACHMENT_THRESHOLD,
    format_footer,
    format_heartbeat,
    redact_sensitive,
    split_for_discord,
)
from bridge.session import Session, SessionManager, claude_client_factory
from bridge.store import ThreadStore

logger = logging.getLogger(__name__)

EDIT_THROTTLE_S = 0.8
SWEEP_INTERVAL_S = 300.0


async def _send(target, content: str | None = None, **kwargs):
    """디스코드로 나가는 유일한 send 경로. content를 가린 뒤 보낸다."""
    if content is None:
        return await target.send(**kwargs)
    return await target.send(redact_sensitive(content), **kwargs)


async def _edit(message, content: str):
    return await message.edit(content=redact_sensitive(content))


def _text_file(text: str, filename: str) -> discord.File:
    """첨부 파일도 가린 뒤 만든다."""
    return discord.File(io.BytesIO(redact_sensitive(text).encode("utf-8")), filename=filename)


# Hard bound on a single streamed turn. `receive_response()`'s own docstring
# is explicit: "If no ResultMessage is received, the iterator continues
# indefinitely." Unbounded, a CLI that stalls or dies without emitting a
# ResultMessage leaves `_handle_prompt` holding `session.lock` forever;
# `sweep_idle` skips lock-held sessions, so the client is never reclaimed and
# every later message in that thread queues behind it, silently, for as long
# as the bot runs.
#
# 90 minutes (5400s), up from 30 minutes: a single turn may legitimately delegate
# work to an external agent (`DELEGATE_TIMEOUT_S`, default 3000s = 50 minutes). The turn timeout must
# exceed the delegate timeout so the delegation can finish and return its result within
# the turn. If DELEGATE_TIMEOUT_S is set larger than RESPONSE_TIMEOUT_S, the turn timeout
# will trip first.
RESPONSE_TIMEOUT_S = 5400.0

# `SessionManager`'s factory signature is fixed as (cwd, resume) -> client: it
# carries no thread id, so `_make_client` below has no parameter through
# which to learn which thread's approval gate to inject. A plain instance
# attribute (`self._current_thread = thread.id` right before `acquire()`)
# would race: `acquire()` suspends across several awaits (the manage lock,
# the connect lock, the actual subprocess connect), and discord.py dispatches
# every event — on_message included — as its own asyncio Task
# (`Client._schedule_event` calls `loop.create_task`), so two threads' handlers
# can genuinely interleave. Two on_message tasks writing the same instance
# attribute could hand thread A's session the gate built for thread B.
#
# A `ContextVar` sidesteps this without a lock: asyncio.Task copies the
# current context at creation time (contextvars.copy_context()), so a value
# set inside one Task's coroutine is invisible to, and never overwritten by,
# a sibling Task -- even though both run on the same event loop and can
# interleave freely. Each on_message dispatch gets its own isolated
# "current thread" slot for free. This also avoids serializing acquire()
# across unrelated threads, which a shared `asyncio.Lock` spanning the
# set+acquire critical section would have forced.
_current_thread_var: contextvars.ContextVar[int] = contextvars.ContextVar(
    "current_thread"
)


class ApprovalView(discord.ui.View):
    """Allow / Allow all / Deny 버튼. 소유자만 누를 수 있다."""

    def __init__(self, owner_id: int, timeout_s: float) -> None:
        super().__init__(timeout=timeout_s)
        self._owner_id = owner_id
        loop = asyncio.get_running_loop()
        self.future: asyncio.Future[Decision] = loop.create_future()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "이 승인은 소유자만 할 수 있습니다.", ephemeral=True
            )
            return False
        return True

    def _settle(self, decision: Decision) -> None:
        if not self.future.done():
            self.future.set_result(decision)
        self.stop()

    async def on_timeout(self) -> None:
        self._settle("deny")

    async def _resolve(
        self, interaction: discord.Interaction, decision: Decision, message: str
    ) -> None:
        # The gate's own `asyncio.wait_for` (bridge/permissions.py) can time
        # out and deny independently of this View's Discord-side timeout. If
        # the owner clicks a button after that already happened, the future
        # is done and `_settle` below would be a no-op -- but the click's
        # `edit_message` above it would still tell the owner "allowed" for a
        # request that was already denied. Check first and show an honest
        # "already resolved" message instead of a misleading one.
        if self.future.done():
            await interaction.response.edit_message(
                content="⌛ 이미 시간 초과로 처리되었습니다.", view=None
            )
            return
        await interaction.response.edit_message(content=message, view=None)
        self._settle(decision)

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.success)
    async def allow(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._resolve(interaction, "allow", "✅ 허용됨")

    @discord.ui.button(
        label="Allow all (this session)", style=discord.ButtonStyle.primary
    )
    async def allow_all(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._resolve(
            interaction, "allow_all", "✅ 이 세션 동안 이 도구 자동 허용"
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._resolve(interaction, "deny", "⛔ 거부됨")


class BridgeBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self.settings = settings
        self.store = ThreadStore(settings.state_dir / "threads.json")
        self.sessions = SessionManager(
            self._make_client,
            self.store,
            idle_timeout=timedelta(hours=settings.idle_timeout_hours),
        )
        self._gates: dict[int, ApprovalGate] = {}
        self._sweep_task: asyncio.Task[None] | None = None

    # --- 인가 ---------------------------------------------------------

    def _is_owner(self, message: discord.Message) -> bool:
        """거부할 때마다 이유를 남긴다.

        세 가지 거부 경로가 전부 조용히 `False`만 반환하던 탓에, 설정이 하나
        틀린 봇과 정상인 봇이 로그에서 똑같아 보였다 -- 게이트웨이에는 붙고,
        `logged in as`도 찍히고, 메시지에만 반응이 없다. 무엇이 걸렀는지
        말해주지 않으면 소유자는 네 가지 설정을 한 번에 의심하게 된다.
        """
        if message.author.id != self.settings.owner_id:
            logger.info(
                "ignored: author %s is not OWNER_ID %s",
                message.author.id,
                self.settings.owner_id,
            )
            return False
        if message.guild is None:
            return True
        if message.guild.id not in self.settings.guild_ids:
            logger.warning(
                "ignored: guild %s is not in GUILD_IDS %s",
                message.guild.id,
                sorted(self.settings.guild_ids),
            )
            return False
        if not self._is_my_channel(message):
            logger.warning(
                "ignored: channel %s is not in CHANNEL_IDS %s",
                self._root_channel_id(message),
                sorted(self.settings.channel_ids),
            )
            return False
        return True

    @staticmethod
    def _root_channel_id(message: discord.Message) -> int:
        channel = message.channel
        if isinstance(channel, discord.Thread):
            return channel.parent_id
        return channel.id

    def _is_my_channel(self, message: discord.Message) -> bool:
        """CHANNEL_IDS가 비면 전부 처리(단일 기기). 설정되면 그 채널만.

        기기마다 같은 토큰으로 붙으면 모든 기기가 모든 메시지를 받는다.
        루트 채널 기준으로 걸러야 스레드 안 대화도 한 기기에만 간다.
        """
        if not self.settings.channel_ids:
            return True
        return self._root_channel_id(message) in self.settings.channel_ids

    # --- 클라이언트 생성 (게이트 주입) --------------------------------

    async def _make_client(self, cwd: Path, resume: str | None) -> Any:
        try:
            thread_id = _current_thread_var.get()
        except LookupError:
            # 프로그래밍 오류로만 발생해야 한다: acquire()를 트리거하는 모든
            # 호출부가 _current_thread_var를 먼저 설정한다. 조용히 아무 게이트나
            # 골라 쓰는 대신 크게 실패한다 — 잘못 골라 쓰면 다른 쓰레드의 승인
            # 게이트로 도구를 실행하게 될 수 있다.
            raise RuntimeError(
                "_make_client invoked without a current-thread context"
            ) from None
        # `.get()`, not `self._gates[thread_id]`: a missing gate here is a
        # bug (every acquire()-triggering call site calls `_ensure_gate`
        # first), never the ordinary "unknown thread" case. Raising
        # `RuntimeError` instead of letting a bare `KeyError` escape keeps
        # this failure from being swallowed by `_handle_prompt`'s
        # `except KeyError`, which exists only to catch
        # `SessionManager.acquire`'s "no cwd for unknown thread" error and
        # would otherwise misreport a real bug as "use !new to start".
        gate = self._gates.get(thread_id)
        if gate is None:
            raise RuntimeError(f"no approval gate registered for thread {thread_id}")
        gate.cwd = cwd
        return await claude_client_factory(
            cwd,
            resume,
            can_use_tool=gate,
            allowed_domains=self.settings.sandbox_allowed_domains,
            state_dir=self.settings.state_dir,
            delegate=self.settings.delegate,
        )

    def _ensure_gate(self, thread: discord.Thread) -> None:
        if thread.id in self._gates:
            return
        prompter = functools.partial(self._prompt_for_approval, thread)
        self._gates[thread.id] = ApprovalGate(
            prompter,
            self.settings.state_dir / "audit.log",
            timeout_s=self.settings.approval_timeout_s,
            on_block=functools.partial(self._notify_block, thread),
            delegate=self.settings.delegate,
        )

    async def _notify_block(self, thread: discord.Thread, summary: str, reason: str) -> None:
        await _send(thread, f"⛔ 차단됨 — {reason}\n{summary}")


    def _drop_gate(self, thread_id: int) -> None:
        """A new session (`!cd`, `!resume`) must never inherit the previous
        session's `ApprovalGate` -- in particular its accumulated
        `allow_all` `_blanket` set (bridge/permissions.py documents one gate
        instance per session; a grant made while trusting one project must
        not silently keep applying after the owner moves to another). The
        caller is responsible for making sure `_ensure_gate` runs again
        before the next `acquire()` for this thread, so `_make_client` never
        observes a dropped-and-not-yet-recreated gate."""
        self._gates.pop(thread_id, None)

    async def _prompt_for_approval(
        self,
        thread: discord.Thread,
        tool_name: str,
        tool_input: dict[str, Any],
        summary: str,
    ) -> Decision:
        view = ApprovalView(self.settings.owner_id, self.settings.approval_timeout_s)
        await _send(thread, f"승인 요청\n{summary}", view=view)
        return await view.future

    async def _acquire_with_gate(
        self, thread_id: int, cwd: Path | None = None
    ) -> Session:
        """`_make_client`이 이 쓰레드의 게이트를 보도록 컨텍스트를 씌운 채
        acquire()를 호출한다. 이 헬퍼를 거치지 않고 `self.sessions.acquire`를
        직접 부르면 안 된다."""
        token = _current_thread_var.set(thread_id)
        try:
            return await self.sessions.acquire(thread_id, cwd)
        finally:
            _current_thread_var.reset(token)

    # --- 이벤트 -------------------------------------------------------

    async def on_ready(self) -> None:
        logger.info("logged in as %s", self.user)
        self._warn_unresolved_ids()
        # discord.py dispatches `ready` on every reconnect, not just the
        # first connect. Without this guard, a bot that reconnects a few
        # times over a long uptime accumulates concurrent `_sweep_loop`
        # tasks, each independently sweeping idle sessions.
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = self.loop.create_task(self._sweep_loop())

    def _warn_unresolved_ids(self) -> None:
        """설정된 서버/채널 id가 실제로 존재하는지 기동할 때 확인한다.

        한 자리 틀린 snowflake도 형식 검사는 통과한다. 그런 봇은 정상으로
        보이다가 메시지에만 반응하지 않으므로, 메시지를 보내봐야 오타를
        알아챌 수 있었다. 여기서 이름을 찍어두면 기동 로그만 보고 끝난다.
        """
        for guild_id in sorted(self.settings.guild_ids):
            guild = self.get_guild(guild_id)
            if guild is None:
                logger.warning(
                    "GUILD_IDS has %s, but the bot is not in a server with "
                    "that id -- check for a typo or invite the bot",
                    guild_id,
                )
            else:
                logger.info("GUILD_IDS %s -> %s", guild_id, guild.name)

        for channel_id in sorted(self.settings.channel_ids):
            channel = self.get_channel(channel_id)
            if channel is None:
                logger.warning(
                    "CHANNEL_IDS has %s, but no visible channel has that id "
                    "-- check for a typo or the bot's channel permissions",
                    channel_id,
                )
            else:
                logger.info("CHANNEL_IDS %s -> #%s", channel_id, channel.name)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            try:
                swept = await self.sessions.sweep_idle()
                if swept:
                    logger.info("swept idle sessions: %s", swept)
            except Exception:
                logger.exception("idle sweep failed")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self._is_owner(message):
            return

        content = message.content.strip()
        if not content and not message.attachments:
            # MESSAGE CONTENT INTENT가 꺼져 있으면 디스코드가 본문을 빈
            # 문자열로 준다. 예외도 경고도 없이 모든 명령이 매칭에 실패하므로,
            # 봇은 정확히 "아무 반응 없음"처럼 보인다. 이 프로젝트에서 가장
            # 흔한 오설정이니 이름을 대고 말해준다.
            logger.warning(
                "received an empty message from the owner: MESSAGE CONTENT "
                "INTENT is probably off in the Discord developer portal "
                "(Bot tab). Every command will be ignored until it is on."
            )
            return
        if content.startswith("!new "):
            await self._cmd_new(message, content[5:].strip())
            return
        if content == "!sessions":
            try:
                listing = self._format_sessions()
            except Exception:
                # Reading threads.json can fail (corrupt file, permissions).
                # Say so in the channel: a command that answers with nothing
                # is indistinguishable from a bot that stopped listening.
                logger.exception("!sessions failed")
                listing = "⚠️ 세션 목록을 읽지 못했습니다. `bot.err.log`를 보세요."
            await _send(message.channel, listing)
            return
        if not isinstance(message.channel, discord.Thread):
            return

        thread = message.channel
        if content == "!stop":
            await self._cmd_stop(thread)
            return
        if content.startswith("!cd "):
            await self._cmd_cd(thread, content[4:].strip())
            return
        if content.startswith("!resume "):
            await self._cmd_resume(thread, content[8:].strip())
            return

        await self._handle_prompt(thread, content)

    # --- 명령 ---------------------------------------------------------

    async def _cmd_new(self, message: discord.Message, raw_path: str) -> None:
        cwd = Path(raw_path or str(self.settings.default_cwd)).expanduser()
        if not cwd.is_dir():
            await _send(message.channel, f"⛔ 디렉터리가 없습니다: `{cwd}`")
            return
        thread = await message.create_thread(name=cwd.name[:90] or "claude")
        # Symmetric with `!cd` and `!resume`: every session-creating path
        # drops any gate the thread id might already carry before building a
        # new one. A brand-new thread id should never already have a gate --
        # but Discord thread ids are only unique in practice, `_gates` is
        # never pruned when a thread is deleted, and inheriting a stale
        # `allow_all` set into what the owner experiences as a brand-new
        # session is exactly the failure `_drop_gate` exists to prevent.
        # Being uniform is cheaper than reasoning about whether this one
        # path is special.
        self._drop_gate(thread.id)
        self._ensure_gate(thread)
        try:
            await self._acquire_with_gate(thread.id, cwd)
        except Exception as exc:
            # The thread exists by now; without this the raise escapes into
            # discord.py's default handler and the owner is left staring at
            # an empty thread that will never say anything.
            logger.exception("!new failed on thread %s", thread.id)
            await self._send_error(thread, exc)
            return
        await _send(thread, f"🟢 세션 시작 — `{cwd}`")

    async def _cmd_cd(self, thread: discord.Thread, raw_path: str) -> None:
        cwd = Path(raw_path).expanduser()
        if not cwd.is_dir():
            await _send(thread, f"⛔ 디렉터리가 없습니다: `{cwd}`")
            return
        # New cwd == new session boundary: drop the old gate (and its
        # accumulated allow_all grants) before rebuilding, then recreate it
        # immediately so the next acquire() never runs without one.
        self._drop_gate(thread.id)
        self._ensure_gate(thread)
        try:
            await self._acquire_with_gate(thread.id, cwd)
        except Exception as exc:
            logger.exception("!cd failed on thread %s", thread.id)
            await self._send_error(thread, exc)
            return
        await _send(thread, f"📂 작업 디렉터리 변경 — `{cwd}` (새 세션)")

    async def _cmd_resume(self, thread: discord.Thread, session_id: str) -> None:
        record = self.store.get(thread.id)
        cwd = record.cwd if record else self.settings.default_cwd
        try:
            await self.sessions.close(thread.id)
        except Exception as exc:
            logger.exception("!resume failed on thread %s", thread.id)
            await self._send_error(thread, exc)
            return
        # Same session-boundary rule as `!cd`: a resumed session is a new
        # session as far as approval scope is concerned, so it gets a fresh
        # gate. `_handle_prompt` calls `_ensure_gate` again before the next
        # message triggers `acquire()`, so there is no window where a
        # message could arrive to find the gate missing.
        self._drop_gate(thread.id)
        self.store.put(thread.id, session_id, cwd)
        await _send(thread, f"↩️ 세션 `{session_id}` 로 복구 예약됨 — 다음 메시지에 적용")

    async def _cmd_stop(self, thread: discord.Thread) -> None:
        session = self.sessions.peek(thread.id)
        if session is None or session.client is None:
            await _send(thread, "실행 중인 작업이 없습니다.")
            return
        try:
            await session.client.interrupt()
        except Exception:
            logger.exception("interrupt failed on thread %s", thread.id)
            await _send(thread, "⚠️ 인터럽트 전송에 실패했습니다.")
            return
        await _send(thread, "🛑 인터럽트 전송됨")

    def _format_sessions(self) -> str:
        active = set(self.sessions.active_threads())
        rows = []
        for thread_id, record in self.store.all().items():
            live = "🟢" if thread_id in active else "⚪"
            rows.append(f"{live} <#{thread_id}> `{record.cwd}` — `{record.session_id}`")
        return "\n".join(rows) or "세션 없음"

    # --- 프롬프트 처리 -------------------------------------------------

    async def _handle_prompt(self, thread: discord.Thread, text: str) -> None:
        # A session whose client is gone is about to be rebuilt via
        # `resume` -- either the idle sweep reclaimed it (hours ago, by
        # definition) or a ProcessError tore it down. permissions.py is
        # explicit that an `allow_all` blanket is "valid only for this
        # instance's lifetime", and the owner's mental model of a lifetime
        # is a sitting, not a process. An "Allow all Bash" tapped three
        # hours before an idle sweep must not still be in force in what
        # feels like a fresh session, so the gate is dropped and rebuilt
        # alongside the client it belonged to.
        existing = self.sessions.peek(thread.id)
        if existing is not None and existing.client is None:
            self._drop_gate(thread.id)
        self._ensure_gate(thread)
        try:
            session = await self._acquire_with_gate(thread.id)
        except KeyError:
            await _send(
                thread,
                "⛔ 이 쓰레드에 세션이 없습니다. `!new <경로>` 로 시작하세요.",
            )
            return
        except Exception as exc:
            # Everything else that can go wrong before a single byte is
            # streamed: CLINotFoundError, a connect() failure, a resume of a
            # session whose .jsonl is gone, a cwd that was deleted since,
            # `_make_client`'s RuntimeError. All of these used to escape
            # into discord.py's default handler and land ONLY in
            # bot.err.log -- so the owner, away from the machine, saw their
            # message sit there with no reply and no error, with no way to
            # tell it from "still thinking". A system built for someone who
            # is not at the machine must not fail silently at the machine.
            logger.exception("acquire failed on thread %s", thread.id)
            await self._send_error(thread, exc)
            return

        if session.lock.locked():
            await _send(thread, "⏳ 이전 요청 처리 중 — 끝나면 이어서 처리합니다.")

        async with session.lock:
            try:
                await self._stream_answer(thread, session, text)
            except ProcessError:
                logger.exception("claude process died on thread %s", thread.id)
                await self.sessions.drop_client(thread.id)
                await _send(
                    thread,
                    "⚠️ 세션이 죽어서 폐기했습니다. 다음 메시지에 resume으로 복구합니다.",
                )
            except Exception as exc:
                logger.exception("prompt failed on thread %s", thread.id)
                await self._send_error(thread, exc)

    async def _stream_answer(
        self, thread: discord.Thread, session: Session, text: str
    ) -> None:
        # `session.client` must be re-read here, under `session.lock` (held by
        # our caller), rather than trusted from whatever `acquire()` returned
        # earlier: a concurrent `!cd` on this same thread can tear the client
        # down and rebuild it while we were merely *waiting* to take
        # `session.lock` (e.g. while a "previous request in progress" notice
        # was being sent, before we entered the `async with`). If that rebuild
        # itself failed (bad cwd, factory error), `session.client` is left
        # None even though the lock is now free for us to take. Re-acquire
        # through the manager instead of dereferencing None.
        client = session.client
        if client is None:
            original_session = session
            # A concurrent `!resume` can have dropped the gate in the window
            # since our caller's `_ensure_gate`; without this, `_make_client`
            # raises a raw RuntimeError instead of rebuilding.
            self._ensure_gate(thread)
            session = await self._acquire_with_gate(thread.id)
            if session is not original_session:
                # `_handle_prompt` is holding `original_session.lock`, not
                # whatever lock this newly-built Session carries. A
                # concurrent `!resume` (or `!cd`, or `!new` racing a stale
                # thread id) can close the old session and have acquire()
                # build a brand new Session object with its own fresh lock
                # while we were reconnecting -- proceeding here would stream
                # this request against a session no one is holding the lock
                # for, silently turning off the serialization
                # `_handle_prompt` thinks it has. Fail loudly instead of
                # continuing on the wrong object.
                raise RuntimeError(
                    f"session for thread {thread.id} was replaced concurrently "
                    "(likely a !resume or !cd) while reconnecting; aborting "
                    "this request rather than streaming without its lock"
                )
            client = session.client
        if client is None:
            raise RuntimeError(
                f"no client available for thread {thread.id} after reconnect"
            )

        await client.query(text)

        placeholder = await _send(thread, "⏳ 작업 중…")
        buffer: list[str] = []
        tool_count = 0
        started = time.monotonic()
        last_edit = 0.0

        async def refresh(force: bool = False) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if not force and now - last_edit < EDIT_THROTTLE_S:
                return
            last_edit = now
            body = "".join(buffer).strip()
            if not body:
                body = format_heartbeat(now - started, tool_count)
            else:
                body = redact_sensitive(body)
            await _edit(placeholder, split_for_discord(body)[0])

        async def drain() -> bool:
            """Returns True if a ResultMessage arrived and was finalized."""
            nonlocal tool_count
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            buffer.append(block.text)
                            await refresh()
                        elif isinstance(block, ToolUseBlock):
                            tool_count += 1
                            # Tool calls and results are deliberately not
                            # posted: on a phone each one is a notification
                            # and they bury the answer. The heartbeat's tool
                            # count is the only trace. A long tool-only
                            # stretch is exactly when a phone user needs the
                            # "still working" placeholder to move -- it must
                            # not sit frozen until the next TextBlock, which
                            # may be minutes away. refresh() is still
                            # throttled to EDIT_THROTTLE_S internally.
                            await refresh()
                elif isinstance(message, UserMessage) and isinstance(
                    message.content, list
                ):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            await refresh()
                elif isinstance(message, ResultMessage):
                    self.sessions.record_session_id(thread.id, message.session_id)
                    await self._finalize(thread, placeholder, buffer, message)
                    return True
            return False

        try:
            finalized = await asyncio.wait_for(drain(), timeout=RESPONSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            # `receive_response()` iterates forever when no ResultMessage
            # ever arrives, and `_handle_prompt` is holding `session.lock`
            # around this call. Left alone that lock is never released:
            # `sweep_idle` skips lock-held sessions, so the client is never
            # reclaimed either, and every subsequent message in this thread
            # queues behind a turn that will never end. Say so plainly, drop
            # the client so the next message rebuilds via resume, and let
            # the lock go by returning.
            logger.error(
                "no ResultMessage within %ss on thread %s; dropping client",
                RESPONSE_TIMEOUT_S,
                thread.id,
            )
            await self.sessions.drop_client(thread.id)
            minutes = int(RESPONSE_TIMEOUT_S // 60)
            await _send(
                thread,
                f"⏱ {minutes}분 동안 응답이 끝나지 않아 중단했습니다. "
                "세션을 폐기했으니 다음 메시지에 resume으로 복구합니다.",
            )
            return

        if not finalized:
            await refresh(force=True)

    async def _finalize(
        self,
        thread: discord.Thread,
        placeholder: discord.Message,
        buffer: list[str],
        result: ResultMessage,
    ) -> None:
        body = "".join(buffer).strip() or (result.result or "(빈 응답)")
        body = redact_sensitive(body)
        footer = format_footer(result)

        if len(body) > ATTACHMENT_THRESHOLD:
            await _edit(
                placeholder,
                content=f"📎 응답이 길어 파일로 첨부합니다.\n{footer}",
            )
            await _send(
                thread,
                file=_text_file(body, "response.md"),
            )
            return

        chunks = split_for_discord(body)
        await _edit(placeholder, content=chunks[0])
        for chunk in chunks[1:]:
            await _send(thread, chunk)
        await _send(thread, footer)

    async def _send_error(self, thread: discord.Thread, exc: Exception) -> None:
        trace = "".join(traceback.format_exception(exc))
        trace = redact_sensitive(trace)
        lines = trace.splitlines()
        head = "\n".join(lines[:20])
        await _send(thread, f"❌ 오류\n```\n{head}\n```")
        if len(lines) > 20:
            await _send(
                thread,
                file=_text_file(trace, "traceback.txt"),
            )

