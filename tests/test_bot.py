"""BridgeBot을 실제 Discord 게이트웨이 없이 검증한다.

`discord.Client.__init__`은 네트워크에 붙지 않으므로 `BridgeBot(settings)`을
직접 생성할 수 있다. `bridge.bot.claude_client_factory`만 가짜로 바꿔서 실제
Claude CLI가 뜨지 않게 한다 (토큰 비용 없는 단위 테스트).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import bridge.bot as botmod
from bridge.bot import ApprovalView, BridgeBot
from bridge.config import Settings


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit(self, content: str | None = None, **_: object) -> None:
        self.edits.append(content or "")


class FakeThread:
    def __init__(self, thread_id: int) -> None:
        self.id = thread_id
        self.sent: list[object] = []
        self.messages: list[FakeMessage] = []
        self.files: list[object] = []
        self.views: list[object] = []

    async def send(self, content: object = None, **kwargs: object) -> FakeMessage:
        self.sent.append(content if content is not None else kwargs)
        if "file" in kwargs:
            self.files.append(kwargs["file"])
        if "view" in kwargs:
            self.views.append(kwargs["view"])
        msg = FakeMessage()
        self.messages.append(msg)
        return msg


class FakeClient:
    def __init__(
        self, cwd: Path, resume: str | None, can_use_tool: object = None
    ) -> None:
        self.cwd = cwd
        self.resume = resume
        self.can_use_tool = can_use_tool
        self.interrupted = False

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def interrupt(self) -> None:
        self.interrupted = True

    async def query(self, text: str) -> None:
        pass

    async def receive_response(self):
        return
        yield  # pragma: no cover - makes this an async generator


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        token="t",
        owner_id=1,
        guild_ids=frozenset({1}),
        channel_ids=frozenset(),
        state_dir=tmp_path / "state",
        default_cwd=tmp_path,
        idle_timeout_hours=2.0,
        approval_timeout_s=1.0,
        sandbox_allowed_domains=frozenset(),
        delegate=None,
    )


def make_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[BridgeBot, list]:
    captured_gates: list = []

    async def fake_factory(cwd, resume, *, can_use_tool=None, **kwargs):
        captured_gates.append(can_use_tool)
        return FakeClient(cwd, resume, can_use_tool)

    monkeypatch.setattr(botmod, "claude_client_factory", fake_factory)
    bot = BridgeBot(make_settings(tmp_path))
    return bot, captured_gates



# --- Finding 1: allow_all must not cross a session boundary --------------


async def test_cd_replaces_gate_and_clears_blanket(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()

    thread = FakeThread(100)
    bot._ensure_gate(thread)
    original_gate = bot._gates[thread.id]
    original_gate._blanket.add("Bash")  # simulate a prior "Allow all" grant

    await bot._cmd_cd(thread, str(dir_b))

    new_gate = bot._gates[thread.id]
    assert new_gate is not original_gate
    assert "Bash" not in new_gate._blanket


async def test_resume_drops_gate_so_next_prompt_gets_a_fresh_one(
    tmp_path, monkeypatch
):
    bot, _ = make_bot(tmp_path, monkeypatch)
    dir_a = tmp_path / "a"
    dir_a.mkdir()

    thread = FakeThread(200)
    bot._ensure_gate(thread)
    await bot._acquire_with_gate(thread.id, dir_a)
    original_gate = bot._gates[thread.id]
    original_gate._blanket.add("Bash")

    await bot._cmd_resume(thread, "some-old-session-id")

    # The gate is gone immediately after !resume -- no stale blanket grant
    # can be reached until a new gate is built.
    assert thread.id not in bot._gates

    # The next message on this thread goes through _handle_prompt, which
    # calls _ensure_gate before acquiring -- proving the gate is rebuilt
    # fresh rather than left missing (which would raise via _make_client).
    await bot._handle_prompt(thread, "hello")
    rebuilt_gate = bot._gates[thread.id]
    assert rebuilt_gate is not original_gate
    assert "Bash" not in rebuilt_gate._blanket


async def test_make_client_raises_runtime_error_when_gate_missing(
    tmp_path, monkeypatch
):
    """A missing gate is a programming bug, never the ordinary "no session
    yet" case that `_handle_prompt`'s `except KeyError` exists to catch. This
    exercises `_make_client` directly: the realistic race (another handler's
    `!cd`/`!resume` dropping the gate in the window between this handler's
    own `_ensure_gate` and the SessionManager actually invoking the factory,
    which happens after internal lock awaits) depends on SessionManager's
    private lock scheduling and isn't something to reach into from here --
    but the guard clause itself, and the fact that it raises `RuntimeError`
    rather than a bare `KeyError` (so `_handle_prompt`'s narrow
    `except KeyError` cannot mistake it for "unknown thread"), is directly
    testable and is what actually matters."""
    bot, _ = make_bot(tmp_path, monkeypatch)
    token = botmod._current_thread_var.set(999)
    try:
        with pytest.raises(RuntimeError, match="no approval gate registered"):
            await bot._make_client(tmp_path, None)
    finally:
        botmod._current_thread_var.reset(token)


async def test_gate_reaches_the_client_factory(tmp_path, monkeypatch):
    """The approval gate is this product's only authority over what executes
    (`setting_sources=[]` means no settings file can pre-allow anything), so
    the wiring that carries it from the thread into the factory is itself a
    security property. `make_bot` has always captured what the factory was
    handed; nothing ever asserted on it.
    """
    bot, captured_gates = make_bot(tmp_path, monkeypatch)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    thread = FakeThread(700)
    bot._ensure_gate(thread)

    session = await bot._acquire_with_gate(thread.id, dir_a)

    gate = bot._gates[thread.id]
    assert captured_gates == [gate]
    # ...and it is the same object the built client actually holds, not just
    # something that passed through the call.
    assert session.client.can_use_tool is gate


# --- Finding 3: heartbeat must move during tool-only stretches ------------


async def test_tool_use_triggers_a_refresh(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    thread = FakeThread(400)
    bot._ensure_gate(thread)
    session = await bot._acquire_with_gate(thread.id, dir_a)

    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

    async def fake_receive_response():
        yield AssistantMessage(
            content=[ToolUseBlock(id="1", name="Bash", input={"command": "ls"})],
            model="test",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            total_cost_usd=None,
            usage=None,
            result="ok",
        )

    queried = []

    async def fake_query(text):
        queried.append(text)

    session.client.query = fake_query
    session.client.receive_response = fake_receive_response

    await bot._stream_answer(thread, session, "do a thing")

    # thread.messages[0] is the placeholder returned by the first
    # thread.send("⏳ 작업 중…") call. It must have been edited more than
    # once: once by the ToolUseBlock-triggered refresh (this test forces
    # force=False through so EDIT_THROTTLE_S would normally suppress a
    # second edit this soon -- so assert on the *first* edit specifically,
    # proving the ToolUseBlock branch reached refresh() at all rather than
    # only ever hitting the force=True call in _finalize).
    placeholder = thread.messages[0]
    assert len(placeholder.edits) >= 1
    first_edit = placeholder.edits[0]
    # The key assertion: refresh() ran while `buffer` was still empty (no
    # TextBlock ever arrived), so its body came from format_heartbeat(),
    # which includes the tool count -- proving the ToolUseBlock branch is
    # what drove this edit rather than it being skipped entirely.
    assert "툴 1회" in first_edit


async def test_tool_activity_is_not_posted_to_thread(tmp_path, monkeypatch):
    # Tool calls and their output flood a phone with notifications and bury
    # the answer; only the placeholder heartbeat should reflect them.
    bot, _ = make_bot(tmp_path, monkeypatch)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    thread = FakeThread(401)
    bot._ensure_gate(thread)
    session = await bot._acquire_with_gate(thread.id, dir_a)

    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    async def fake_receive_response():
        yield AssistantMessage(
            content=[ToolUseBlock(id="1", name="Bash", input={"command": "git status"})],
            model="test",
        )
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="1", content="On branch main")]
        )
        yield AssistantMessage(content=[TextBlock(text="clean")], model="test")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            total_cost_usd=None,
            usage=None,
            result="clean",
        )

    async def fake_query(text):
        pass

    session.client.query = fake_query
    session.client.receive_response = fake_receive_response

    await bot._stream_answer(thread, session, "status?")

    # placeholder + footer only
    assert len(thread.sent) == 2
    assert not any("git status" in str(s) or "On branch" in str(s) for s in thread.sent)
    assert thread.messages[0].edits[-1] == "clean"


# --- Cheap fixes -----------------------------------------------------------


async def test_stop_guards_interrupt_failure(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    thread = FakeThread(500)
    bot._ensure_gate(thread)
    session = await bot._acquire_with_gate(thread.id, dir_a)

    async def boom():
        raise RuntimeError("client already torn down")

    session.client.interrupt = boom

    await bot._cmd_stop(thread)  # must not raise

    assert any("실패" in str(m) for m in thread.sent)


async def test_stream_answer_fails_loudly_on_session_identity_change(
    tmp_path, monkeypatch
):
    bot, _ = make_bot(tmp_path, monkeypatch)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    thread = FakeThread(600)
    bot._ensure_gate(thread)
    session = await bot._acquire_with_gate(thread.id, dir_a)

    # Simulate a concurrent !resume: the client is gone (as if torn down)
    # AND the manager will now hand back a brand new Session object.
    session.client = None
    await bot.sessions.close(thread.id)
    bot.store.put(thread.id, "resumed-id", dir_a)

    with pytest.raises(RuntimeError, match="replaced concurrently"):
        await bot._stream_answer(thread, session, "hello")


async def test_on_ready_does_not_stack_sweep_tasks(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    bot.loop = asyncio.get_running_loop()

    await bot.on_ready()
    first_task = bot._sweep_task
    assert first_task is not None
    assert not first_task.done()

    await bot.on_ready()
    second_task = bot._sweep_task

    assert second_task is first_task  # no second task stacked on top

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task


async def test_late_approval_click_after_timeout_shows_expired_not_allowed():
    view = ApprovalView(owner_id=1, timeout_s=60.0)
    view.future.set_result("deny")  # simulate the gate's own timeout firing

    class FakeResponse:
        def __init__(self) -> None:
            self.edits: list[dict] = []

        async def edit_message(self, **kwargs):
            self.edits.append(kwargs)

    class FakeInteraction:
        def __init__(self) -> None:
            self.response = FakeResponse()

    interaction = FakeInteraction()
    await view._resolve(interaction, "allow", "✅ 허용됨")

    assert interaction.response.edits[-1]["content"] != "✅ 허용됨"
    assert "이미" in interaction.response.edits[-1]["content"]


# --- Whole-branch review: nothing may fail silently on the phone ---------


async def test_prompt_reports_acquire_failure_to_the_thread(tmp_path, monkeypatch):
    """Anything that goes wrong before streaming begins -- CLINotFoundError,
    a connect failure, a resume of a missing session, a deleted cwd,
    `_make_client`'s RuntimeError -- used to escape into discord.py's
    default handler and reach only bot.err.log. The owner on LTE saw their
    message sit there with no reply and no error."""
    bot, _ = make_bot(tmp_path, monkeypatch)
    thread = FakeThread(800)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("claude CLI not found")

    monkeypatch.setattr(bot, "_acquire_with_gate", boom)

    await bot._handle_prompt(thread, "hello")  # must not raise

    assert any("오류" in str(m) for m in thread.sent)
    assert any("claude CLI not found" in str(m) for m in thread.sent)


async def test_new_reports_failure_instead_of_leaving_an_empty_thread(
    tmp_path, monkeypatch
):
    bot, _ = make_bot(tmp_path, monkeypatch)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    thread = FakeThread(810)

    class FakeChannelMessage:
        def __init__(self) -> None:
            self.channel = FakeThread(811)

        async def create_thread(self, name: str):
            return thread

    async def boom(*_args, **_kwargs):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(bot, "_acquire_with_gate", boom)

    await bot._cmd_new(FakeChannelMessage(), str(dir_a))  # must not raise

    assert any("오류" in str(m) for m in thread.sent)


async def test_stream_answer_is_bounded_and_drops_the_client(tmp_path, monkeypatch):
    """`receive_response()` iterates forever if no ResultMessage arrives.
    Unbounded, `_handle_prompt` holds `session.lock` forever, `sweep_idle`
    skips lock-held sessions, and every later message in the thread queues
    behind a turn that never ends."""
    bot, _ = make_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(botmod, "RESPONSE_TIMEOUT_S", 0.05)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    thread = FakeThread(820)
    bot._ensure_gate(thread)
    session = await bot._acquire_with_gate(thread.id, dir_a)

    async def never_finishes():
        await asyncio.sleep(30)
        yield  # pragma: no cover

    async def fake_query(_text):
        pass

    session.client.query = fake_query
    session.client.receive_response = never_finishes

    await bot._stream_answer(thread, session, "long thing")  # must not hang

    assert any("⏱" in str(m) for m in thread.sent)
    # The client is dropped so the next message rebuilds via resume rather
    # than reusing a wedged one.
    assert bot.sessions.peek(thread.id).client is None


async def test_idle_resume_rebuilds_the_gate(tmp_path, monkeypatch):
    """permissions.py says an `allow_all` blanket is valid only for this
    gate instance's lifetime. After a 2-hour idle sweep the owner
    experiences a new sitting, so a grant tapped hours earlier must not
    still apply."""
    bot, _ = make_bot(tmp_path, monkeypatch)
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    thread = FakeThread(830)
    bot._ensure_gate(thread)
    await bot._acquire_with_gate(thread.id, dir_a)
    stale_gate = bot._gates[thread.id]
    stale_gate._blanket.add("Bash")

    await bot.sessions.drop_client(thread.id)  # what sweep_idle leaves behind

    await bot._handle_prompt(thread, "hello again")

    rebuilt = bot._gates[thread.id]
    assert rebuilt is not stale_gate
    assert "Bash" not in rebuilt._blanket


def _fake_thread(thread_id: int, parent_id: int):
    """isinstance(x, discord.Thread)를 통과하는 최소 스텁."""
    import discord

    thread = object.__new__(discord.Thread)
    object.__setattr__(thread, "id", thread_id)
    object.__setattr__(thread, "parent_id", parent_id)
    return thread


def test_channel_filter_routes_threads_by_parent(tmp_path, monkeypatch):
    """CHANNEL_IDS가 설정되면 다른 채널(및 그 스레드)은 무시한다."""
    bot, _ = make_bot(tmp_path, monkeypatch)
    object.__setattr__(bot.settings, "channel_ids", frozenset({100}))

    mine = SimpleNamespace(id=100, parent_id=None)
    theirs = SimpleNamespace(id=200, parent_id=None)
    my_thread = _fake_thread(999, 100)
    other_thread = _fake_thread(998, 200)

    def msg(channel):
        return SimpleNamespace(
            author=SimpleNamespace(id=1, bot=False),
            guild=SimpleNamespace(id=1),
            channel=channel,
        )

    assert bot._is_owner(msg(mine)) is True
    assert bot._is_owner(msg(theirs)) is False
    assert bot._is_owner(msg(my_thread)) is True
    assert bot._is_owner(msg(other_thread)) is False

    object.__setattr__(bot.settings, "channel_ids", frozenset())
    assert bot._is_owner(msg(theirs)) is True


def test_rejections_say_which_setting_rejected(tmp_path, monkeypatch, caplog):
    """조용한 거부는 오설정과 정상을 구분할 수 없게 만든다."""
    import logging

    bot, _ = make_bot(tmp_path, monkeypatch)
    object.__setattr__(bot.settings, "channel_ids", frozenset({100}))

    def msg(author_id: int, guild_id: int, channel_id: int):
        return SimpleNamespace(
            author=SimpleNamespace(id=author_id, bot=False),
            guild=SimpleNamespace(id=guild_id),
            channel=SimpleNamespace(id=channel_id, parent_id=None),
        )

    for message, needle in [
        (msg(999, 1, 100), "OWNER_ID"),
        (msg(1, 42, 100), "GUILD_IDS"),
        (msg(1, 1, 200), "CHANNEL_IDS"),
    ]:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="bridge.bot"):
            assert bot._is_owner(message) is False
        assert needle in caplog.text


def test_startup_warns_about_ids_that_resolve_to_nothing(
    tmp_path, monkeypatch, caplog
):
    """한 자리 틀린 snowflake는 형식 검사를 통과한다. 기동 때 잡는다."""
    import logging

    bot, _ = make_bot(tmp_path, monkeypatch)
    object.__setattr__(bot.settings, "channel_ids", frozenset({100, 404}))
    monkeypatch.setattr(
        type(bot), "get_guild", lambda self, gid: SimpleNamespace(name="srv")
    )
    monkeypatch.setattr(
        type(bot),
        "get_channel",
        lambda self, cid: SimpleNamespace(name="ops") if cid == 100 else None,
    )

    with caplog.at_level(logging.INFO, logger="bridge.bot"):
        bot._warn_unresolved_ids()

    assert "CHANNEL_IDS 100 -> #ops" in caplog.text
    assert "CHANNEL_IDS has 404" in caplog.text


# --- Task 5: Bot wiring & block notification -------------------------------


async def test_make_client_sets_gate_cwd_and_passes_settings(tmp_path, monkeypatch):
    captured_kwargs = {}
    async def recording_factory(cwd, resume, *, can_use_tool=None, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeClient(cwd, resume, can_use_tool)

    monkeypatch.setattr(botmod, "claude_client_factory", recording_factory)
    bot = BridgeBot(make_settings(tmp_path))
    dir_a = tmp_path / "a"
    dir_a.mkdir()

    thread = FakeThread(201)
    bot._ensure_gate(thread)
    session = await bot._acquire_with_gate(thread.id, dir_a)
    gate = bot._gates[thread.id]

    assert gate.cwd == dir_a
    assert captured_kwargs.get("allowed_domains") == bot.settings.sandbox_allowed_domains
    assert captured_kwargs.get("state_dir") == bot.settings.state_dir


async def test_notify_block_sends_message_to_thread(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    thread = FakeThread(301)
    bot._ensure_gate(thread)
    gate = bot._gates[thread.id]
    assert gate._on_block is not None

    await gate._on_block("🔧 WebFetch: `http://example.com`", "외부 접속 차단")
    assert len(thread.sent) == 1
    assert thread.sent[0].startswith("⛔ 차단됨 — 외부 접속 차단")
    assert "WebFetch" in thread.sent[0]


# --- Task 2: Output Gateway & Redaction ------------------------------------

import ast
from claude_agent_sdk import TextBlock, AssistantMessage, ResultMessage


def test_bot_no_gateway_bypass():
    """ast로 bridge/bot.py를 파싱해 관문 우회 호출이 없는지 검사."""
    bot_path = Path("bridge/bot.py")
    tree = ast.parse(bot_path.read_text("utf-8"), filename=str(bot_path))

    allowed_functions = {"_send", "_edit", "_text_file"}
    violations = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.current_function = None

        def visit_FunctionDef(self, node):
            old = self.current_function
            self.current_function = node.name
            self.generic_visit(node)
            self.current_function = old

        def visit_AsyncFunctionDef(self, node):
            old = self.current_function
            self.current_function = node.name
            self.generic_visit(node)
            self.current_function = old

        def visit_Call(self, node):
            # 허용된 헬퍼 함수 내부 호출은 통과
            if self.current_function in allowed_functions:
                self.generic_visit(node)
                return

            # discord.File(...) 직접 호출 검사
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == "File" and isinstance(node.func.value, ast.Name) and node.func.value.id == "discord":
                    violations.append(
                        f"Line {node.lineno}: direct discord.File call outside _text_file"
                    )

            # .send(...) 또는 .edit(...) 호출 검사
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"send", "edit"}:
                # interaction.response 속성 체인은 허용
                val = node.func.value
                if isinstance(val, ast.Attribute) and val.attr == "response":
                    if isinstance(val.value, ast.Name) and val.value.id == "interaction":
                        self.generic_visit(node)
                        return
                violations.append(
                    f"Line {node.lineno}: direct .{node.func.attr}() call outside gateway helper"
                )

            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    assert not violations, "Gateway bypass calls found:\n" + "\n".join(violations)


async def test_stream_answer_redacts_sensitive(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    thread = FakeThread(401)
    session = SimpleNamespace(client=FakeClient(tmp_path, None))

    async def fake_receive():
        yield AssistantMessage(
            content=[TextBlock(text="password=hunter2 서버 10.0.3.15")],
            model="test",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="s1",
            total_cost_usd=0.01,
        )

    session.client.receive_response = fake_receive
    session.client.query = lambda text: asyncio.sleep(0)

    await bot._stream_answer(thread, session, "hi")

    # placeholder message's edits
    assert len(thread.messages) > 0
    placeholder = thread.messages[0]
    final_content = placeholder.edits[-1]
    assert "hunter2" not in final_content
    assert "10.0.3.15" not in final_content
    assert "password=[가림]" in final_content
    assert "[IP 가림]" in final_content


async def test_finalize_long_body_attachment_redacted(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    thread = FakeThread(402)
    placeholder = FakeMessage()

    # Create long body > 6000 chars with secret
    secret = "DB_PASSWORD: supersecret999"
    body = secret + "\n" + ("x" * 6500)
    result = ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=1,
        session_id="s2",
        total_cost_usd=0.01,
    )

    await bot._finalize(thread, placeholder, [body], result)

    assert len(thread.files) == 1
    file_obj = thread.files[0]
    file_bytes = file_obj.fp.getvalue().decode("utf-8")
    assert "supersecret999" not in file_bytes
    assert "DB_PASSWORD: [가림]" in file_bytes


async def test_finalize_chunk_boundary_redacted(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    thread = FakeThread(403)
    placeholder = FakeMessage()

    # 1990 chars padding then password=hunter2
    body = ("a" * 1990) + " password=hunter2"
    result = ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=1,
        session_id="s3",
        total_cost_usd=0.01,
    )

    await bot._finalize(thread, placeholder, [body], result)

    all_sent_texts = [placeholder.edits[-1]] + [
        msg for msg in thread.sent if isinstance(msg, str)
    ]
    for text in all_sent_texts:
        assert "hunter2" not in text
    combined = "".join(all_sent_texts)
    assert "password=[가림]" in combined


async def test_send_error_redacted(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    thread = FakeThread(404)

    exc = Exception("connect failed to postgres://u:pw@10.1.1.1/db")
    await bot._send_error(thread, exc)

    assert len(thread.sent) >= 1
    error_msg = thread.sent[0]
    assert "u:pw@" not in error_msg
    assert "10.1.1.1" not in error_msg
    assert "postgres://u:[가림]@[IP 가림]/db" in error_msg


async def test_notify_block_redacted(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    thread = FakeThread(405)

    await bot._notify_block(thread, "connecting to 192.168.1.100:8080", "위험 명령")
    assert len(thread.sent) == 1
    sent_text = thread.sent[0]
    assert "192.168.1.100" not in sent_text
    assert "[IP 가림]:8080" in sent_text


async def test_prompt_for_approval_redacted(tmp_path, monkeypatch):
    bot, _ = make_bot(tmp_path, monkeypatch)
    thread = FakeThread(406)

    # _prompt_for_approval returns a future waiting for decision
    task = asyncio.create_task(
        bot._prompt_for_approval(thread, "Bash", {}, "run with api_key: 12345678")
    )
    # yield to let the coroutine run up to the send
    await asyncio.sleep(0)

    assert len(thread.sent) == 1
    sent_text = thread.sent[0]
    assert "12345678" not in sent_text
    assert "api_key: [가림]" in sent_text
    assert len(thread.views) == 1

    # Cleanup task
    view = thread.views[0]
    view.future.set_result("allow")
    await task


