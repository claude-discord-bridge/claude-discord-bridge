import asyncio
from datetime import timedelta

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from bridge.session import SessionManager, claude_client_factory
from bridge.store import ThreadStore

pytestmark = pytest.mark.integration

# The real CLI can stall or die without ever emitting a ResultMessage;
# receive_response() then iterates forever (see its own docstring). Bound
# every drain so a stuck subprocess fails the test instead of hanging it.
RESPONSE_TIMEOUT = 120


async def ask(session, text: str) -> tuple[str, str]:
    """한 번 물어보고 (본문, session_id)를 돌려준다."""
    await session.client.query(text)

    async def _drain() -> tuple[str, str]:
        body: list[str] = []
        session_id = ""
        async for message in session.client.receive_response():
            if isinstance(message, AssistantMessage):
                body.extend(
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                )
            elif isinstance(message, ResultMessage):
                session_id = message.session_id
        return "\n".join(body), session_id

    try:
        return await asyncio.wait_for(_drain(), timeout=RESPONSE_TIMEOUT)
    except asyncio.TimeoutError:
        pytest.fail(
            f"claude CLI did not emit a ResultMessage within {RESPONSE_TIMEOUT}s "
            f"for query {text!r} (no context to prove context persisted)"
        )


async def test_same_client_keeps_context(tmp_path):
    store = ThreadStore(tmp_path / "threads.json")
    manager = SessionManager(
        claude_client_factory, store, idle_timeout=timedelta(hours=2)
    )
    session = await manager.acquire(1, tmp_path)
    try:
        first, session_id = await ask(session, "2 + 2는? 숫자만 답해.")
        assert "4" in first
        manager.record_session_id(1, session_id)

        second, _ = await ask(session, "방금 내가 물어본 계산식을 그대로 다시 적어줘.")
        assert "2 + 2" in second.replace("  ", " ") or "2+2" in second.replace(" ", "")
    finally:
        await manager.close(1)


async def test_resume_restores_context_after_restart(tmp_path):
    store_path = tmp_path / "threads.json"

    manager = SessionManager(
        claude_client_factory, ThreadStore(store_path), idle_timeout=timedelta(hours=2)
    )
    session = await manager.acquire(1, tmp_path)
    try:
        _, session_id = await ask(session, "내가 제일 좋아하는 섬 이름은 zanzibar야. 기억해.")
        manager.record_session_id(1, session_id)
    finally:
        await manager.close(1)

    # 봇 재기동을 흉내낸다: 새 매니저, 새 스토어 인스턴스, 디스크만 공유.
    revived = SessionManager(
        claude_client_factory, ThreadStore(store_path), idle_timeout=timedelta(hours=2)
    )
    session2 = await revived.acquire(1)
    try:
        answer, _ = await ask(session2, "내가 제일 좋아하는 섬 이름이 뭐였지? 이름만 답해.")
        assert "zanzibar" in answer.lower()
    finally:
        await revived.close(1)
