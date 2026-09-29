import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bridge.session import SessionManager
from bridge.store import ThreadStore


class FakeClient:
    def __init__(self, cwd: Path, resume: str | None) -> None:
        self.cwd = cwd
        self.resume = resume
        self.connected = False
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_calls += 1


def make_manager(tmp_path, idle_hours: float = 2.0):
    created: list[FakeClient] = []

    async def factory(cwd: Path, resume: str | None) -> FakeClient:
        client = FakeClient(cwd, resume)
        created.append(client)
        return client

    store = ThreadStore(tmp_path / "threads.json")
    manager = SessionManager(
        factory, store, idle_timeout=timedelta(hours=idle_hours)
    )
    return manager, created, store


async def test_acquire_creates_and_connects_client(tmp_path):
    manager, created, _ = make_manager(tmp_path)
    session = await manager.acquire(1, Path("/tmp/proj"))
    assert session.client is created[0]
    assert created[0].connected is True
    assert created[0].resume is None


async def test_second_acquire_reuses_same_client(tmp_path):
    manager, created, _ = make_manager(tmp_path)
    first = await manager.acquire(1, Path("/tmp/proj"))
    second = await manager.acquire(1)
    assert first is second
    assert len(created) == 1


async def test_acquire_without_cwd_on_unknown_thread_raises(tmp_path):
    manager, _, _ = make_manager(tmp_path)
    with pytest.raises(KeyError):
        await manager.acquire(99)


async def test_session_id_is_persisted(tmp_path):
    manager, _, store = make_manager(tmp_path)
    await manager.acquire(1, Path("/tmp/proj"))
    manager.record_session_id(1, "sess-xyz")
    record = store.get(1)
    assert record is not None
    assert record.session_id == "sess-xyz"


async def test_idle_sweep_disconnects_but_keeps_session_id(tmp_path):
    manager, created, store = make_manager(tmp_path, idle_hours=1.0)
    await manager.acquire(1, Path("/tmp/proj"))
    manager.record_session_id(1, "sess-xyz")

    future = datetime.now(timezone.utc) + timedelta(hours=3)
    swept = await manager.sweep_idle(now=future)

    assert swept == [1]
    assert created[0].disconnect_calls == 1
    assert store.get(1).session_id == "sess-xyz"


async def test_reacquire_after_sweep_resumes(tmp_path):
    manager, created, _ = make_manager(tmp_path, idle_hours=1.0)
    await manager.acquire(1, Path("/tmp/proj"))
    manager.record_session_id(1, "sess-xyz")
    await manager.sweep_idle(now=datetime.now(timezone.utc) + timedelta(hours=3))

    await manager.acquire(1)

    assert len(created) == 2
    assert created[1].resume == "sess-xyz"
    assert created[1].cwd == Path("/tmp/proj")


async def test_cold_start_from_disk_resumes(tmp_path):
    manager, _, _ = make_manager(tmp_path)
    await manager.acquire(1, Path("/tmp/proj"))
    manager.record_session_id(1, "sess-xyz")

    fresh, created2, _ = make_manager(tmp_path)
    session = await fresh.acquire(1)

    assert created2[0].resume == "sess-xyz"
    assert session.cwd == Path("/tmp/proj")


async def test_lock_serializes_same_thread(tmp_path):
    """Concurrent acquire() calls for the SAME thread must not race: a real
    factory suspends (subprocess spawn), so two overlapping acquire() calls
    must still produce exactly one client and hand back the same Session."""
    created: list[FakeClient] = []

    async def factory(cwd: Path, resume: str | None) -> FakeClient:
        await asyncio.sleep(0.01)
        client = FakeClient(cwd, resume)
        created.append(client)
        return client

    store = ThreadStore(tmp_path / "threads.json")
    manager = SessionManager(factory, store, idle_timeout=timedelta(hours=2))

    first, second = await asyncio.gather(
        manager.acquire(1, Path("/tmp/proj")),
        manager.acquire(1, Path("/tmp/proj")),
    )

    assert first is second
    assert len(created) == 1
    assert created[0].connected is True


async def test_connect_failure_leaves_client_none_for_retry(tmp_path):
    calls = {"n": 0}

    class FlakyClient(FakeClient):
        async def connect(self) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            await super().connect()

    created: list[FlakyClient] = []

    async def factory(cwd: Path, resume: str | None) -> FlakyClient:
        client = FlakyClient(cwd, resume)
        created.append(client)
        return client

    store = ThreadStore(tmp_path / "threads.json")
    manager = SessionManager(factory, store, idle_timeout=timedelta(hours=2))

    with pytest.raises(RuntimeError):
        await manager.acquire(1, Path("/tmp/proj"))

    session = await manager.acquire(1)

    assert session.client is created[1]
    assert created[1].connected is True


async def test_sweep_idle_tolerates_new_thread_created_during_disconnect(tmp_path):
    """sweep_idle must not raise `dictionary changed size during iteration`
    when disconnecting one thread's client triggers, indirectly, the
    creation of another thread's session (e.g. a message arriving on a
    different thread while the sweep is in flight)."""
    spawned = {"done": False}
    manager_ref: dict[str, SessionManager] = {}

    class SpawningClient(FakeClient):
        async def disconnect(self) -> None:
            await super().disconnect()
            if not spawned["done"]:
                spawned["done"] = True
                await manager_ref["manager"].acquire(2, Path("/tmp/b"))

    async def factory(cwd: Path, resume: str | None) -> SpawningClient:
        return SpawningClient(cwd, resume)

    store = ThreadStore(tmp_path / "threads.json")
    manager = SessionManager(factory, store, idle_timeout=timedelta(hours=1))
    manager_ref["manager"] = manager

    await manager.acquire(1, Path("/tmp/a"))

    future = datetime.now(timezone.utc) + timedelta(hours=3)
    swept = await manager.sweep_idle(now=future)

    assert swept == [1]
    assert manager.active_threads() == [2]


async def test_cwd_change_does_not_adopt_a_session_id_from_the_old_cwd(tmp_path):
    """A `!cd` racing an in-flight request must not resume the OLD project's
    conversation rooted in the NEW directory.

    The real race this guards: `_stream_answer` holds `session.lock` for the
    whole turn and calls `record_session_id` (which takes no lock at all) on
    every ResultMessage. If the cwd mutation happened outside `session.lock`
    -- as it used to -- that call could land *after* the swap, writing the
    old directory's session id against the new cwd, and the rebuilt client
    would then resume it. The persisted mapping would point at a session
    whose `.jsonl` lives under a different project slug, so resume fails and
    the thread dies silently.
    """
    manager, created, store = make_manager(tmp_path)

    session = await manager.acquire(1, Path("/tmp/a"))

    in_flight_started = asyncio.Event()
    let_it_finish = asyncio.Event()

    async def in_flight_request() -> None:
        # Exactly what `_handle_prompt`/`_stream_answer` do: hold
        # session.lock across the turn, then record the id on ResultMessage.
        async with session.lock:
            in_flight_started.set()
            await let_it_finish.wait()
            manager.record_session_id(1, "sess-from-old-cwd")

    turn = asyncio.create_task(in_flight_request())
    await in_flight_started.wait()

    cd = asyncio.create_task(manager.acquire(1, Path("/tmp/b")))
    await asyncio.sleep(0.01)  # let the !cd reach its wait on session.lock
    let_it_finish.set()
    await turn
    await cd

    record = store.get(1)
    assert record.cwd == Path("/tmp/b")
    # The invariant: an id recorded while the session was rooted at /tmp/a
    # must not survive into the /tmp/b mapping.
    assert record.session_id != "sess-from-old-cwd"
    assert record.session_id is None
    # ...and the rebuilt client must agree with what was persisted: a fresh
    # session in /tmp/b, not a resume of the /tmp/a conversation. The old
    # version of this test never asserted `resume` at all, which is exactly
    # how it managed to enshrine the broken behaviour.
    assert created[1].cwd == Path("/tmp/b")
    assert created[1].resume is None


async def test_close_disconnects_and_forgets(tmp_path):
    manager, created, _ = make_manager(tmp_path)
    await manager.acquire(1, Path("/tmp/proj"))
    await manager.close(1)
    assert created[0].disconnect_calls == 1
    assert manager.active_threads() == []


async def test_cwd_change_replaces_client(tmp_path):
    manager, created, _ = make_manager(tmp_path)
    await manager.acquire(1, Path("/tmp/a"))
    await manager.acquire(1, Path("/tmp/b"))
    assert len(created) == 2
    assert created[0].disconnect_calls == 1
    assert created[1].cwd == Path("/tmp/b")


async def test_cwd_change_waits_for_in_flight_request(tmp_path):
    """A `!cd` arriving while a previous message is still streaming on the
    same thread must not disconnect the live client out from under it — it
    has to wait for whoever holds `session.lock` to release it first."""
    manager, created, _ = make_manager(tmp_path)
    session = await manager.acquire(1, Path("/tmp/a"))

    holder_acquired = asyncio.Event()
    release_holder = asyncio.Event()

    async def holder() -> None:
        async with session.lock:
            holder_acquired.set()
            await release_holder.wait()

    holder_task = asyncio.create_task(holder())
    await holder_acquired.wait()

    acquire_task = asyncio.create_task(manager.acquire(1, Path("/tmp/b")))
    # Give acquire() a chance to run up to (and block on) session.lock.
    await asyncio.sleep(0.01)

    assert not acquire_task.done()
    assert created[0].disconnect_calls == 0

    release_holder.set()
    await holder_task
    new_session = await acquire_task

    assert created[0].disconnect_calls == 1
    assert new_session.cwd == Path("/tmp/b")


async def test_acquire_for_connected_thread_does_not_wait_on_other_threads_connect(
    tmp_path,
):
    """acquire() for an already-connected thread must return immediately
    even while a different thread is stuck mid-connect — _manage_lock must
    not be held across connect()/disconnect()."""
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    class SlowConnectClient(FakeClient):
        async def connect(self) -> None:
            connect_started.set()
            await release_connect.wait()
            await super().connect()

    created: list[FakeClient] = []

    async def factory(cwd: Path, resume: str | None):
        if cwd == Path("/tmp/slow"):
            client: FakeClient = SlowConnectClient(cwd, resume)
        else:
            client = FakeClient(cwd, resume)
        created.append(client)
        return client

    store = ThreadStore(tmp_path / "threads.json")
    manager = SessionManager(factory, store, idle_timeout=timedelta(hours=2))

    # Thread 2 connects fully first.
    await manager.acquire(2, Path("/tmp/fast"))

    # Thread 1 starts connecting and gets stuck inside connect().
    slow_task = asyncio.create_task(manager.acquire(1, Path("/tmp/slow")))
    await connect_started.wait()

    # Thread 2's re-acquire must not wait on thread 1's stuck connect().
    fast_result = await asyncio.wait_for(manager.acquire(2), timeout=0.5)

    assert fast_result.client is created[0]

    release_connect.set()
    await slow_task


async def test_cwd_change_never_exposes_client_none_under_session_lock(tmp_path):
    """A cwd change is one atomic swap: the disconnect and the reconnect
    must both happen under session.lock, so a task that takes session.lock
    (e.g. to begin streaming a request) can never observe session.client as
    None mid-swap — it either sees the pre-change client (lock not yet
    taken by the change) or waits until the new one is fully attached."""
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    class SlowConnectClient(FakeClient):
        async def connect(self) -> None:
            connect_started.set()
            await release_connect.wait()
            await super().connect()

    created: list[FakeClient] = []

    async def factory(cwd: Path, resume: str | None):
        client: FakeClient
        if cwd == Path("/tmp/b"):
            client = SlowConnectClient(cwd, resume)
        else:
            client = FakeClient(cwd, resume)
        created.append(client)
        return client

    store = ThreadStore(tmp_path / "threads.json")
    manager = SessionManager(factory, store, idle_timeout=timedelta(hours=2))

    session = await manager.acquire(1, Path("/tmp/a"))

    change_task = asyncio.create_task(manager.acquire(1, Path("/tmp/b")))
    # Wait until the old client is torn down and the new one is stuck
    # mid-connect — this is exactly the window where session.client is None.
    await connect_started.wait()

    observed_client: object = "not-yet-observed"

    async def observer() -> None:
        nonlocal observed_client
        async with session.lock:
            observed_client = session.client

    observer_task = asyncio.create_task(observer())
    # Give the observer a chance to run and try to take session.lock while
    # the change is still stuck mid-connect.
    await asyncio.sleep(0.01)

    release_connect.set()
    await change_task
    await observer_task

    assert observed_client is not None


async def test_peek_does_not_create_session(tmp_path):
    manager, created, _ = make_manager(tmp_path)
    assert manager.peek(1) is None
    assert created == []


async def test_peek_returns_live_session(tmp_path):
    manager, _, _ = make_manager(tmp_path)
    session = await manager.acquire(1, Path("/tmp/proj"))
    assert manager.peek(1) is session
