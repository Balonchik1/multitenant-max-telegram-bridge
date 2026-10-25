import asyncio

import aiosqlite
import pytest

import bridge
import state
import tenants


@pytest.fixture(autouse=True)
def clean_resuming():
    state.tenants_resuming.clear()
    yield
    state.tenants_resuming.clear()


async def test_ready_immediately_when_admin_connected_and_no_tenants_waiting():
    admin_ready = asyncio.Event()
    admin_ready.set()
    assert await bridge.wait_until_max_ready(admin_ready, set(), timeout=1) is True


async def test_waits_for_admin_connection():
    admin_ready = asyncio.Event()
    asyncio.get_running_loop().call_later(0.1, admin_ready.set)
    assert await bridge.wait_until_max_ready(admin_ready, set(), timeout=2) is True
    assert admin_ready.is_set()


async def test_waits_until_tenant_groups_are_back():
    admin_ready = asyncio.Event()
    admin_ready.set()
    state.tenants_resuming.add(111)
    asyncio.get_running_loop().call_later(0.3, state.tenants_resuming.discard, 111)

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await bridge.wait_until_max_ready(admin_ready, set(), timeout=3) is True
    assert loop.time() - started >= 0.25  # реально дождались, а не проскочили


async def test_gives_up_after_timeout_instead_of_hanging():
    admin_ready = asyncio.Event()  # MAX так и не подключился
    assert await bridge.wait_until_max_ready(admin_ready, set(), timeout=0.2) is False


async def test_stuck_tenant_does_not_block_forever():
    admin_ready = asyncio.Event()
    admin_ready.set()
    state.tenants_resuming.add(222)
    assert await bridge.wait_until_max_ready(admin_ready, set(), timeout=0.3) is False


async def test_stops_waiting_when_client_task_dies_or_stop_requested():
    admin_ready = asyncio.Event()

    async def crash():
        raise RuntimeError("chats.outage")

    task = asyncio.create_task(crash())
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await bridge.wait_until_max_ready(admin_ready, {task}, timeout=5) is False
    assert loop.time() - started < 1  # не ждём весь таймаут
    assert task.exception() is not None  # исключение прочитано, предупреждений не будет


async def test_ready_check_task_is_cancelled_after_wait():
    admin_ready = asyncio.Event()
    before = len(asyncio.all_tasks())
    await bridge.wait_until_max_ready(admin_ready, set(), timeout=0.1)
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) <= before


async def test_only_active_tenants_are_waited_for(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", str(tmp_path / "bridge.db"))
    await tenants.init_tenants_table()
    async with aiosqlite.connect(state.DB_PATH) as db:
        for owner_id, status in ((1, "active"), (2, "awaiting_login"), (3, "awaiting_group"), (4, "failed")):
            await db.execute(
                "INSERT INTO tenant_accounts (owner_id, max_phone, status) VALUES (?, '7900', ?)",
                (owner_id, status),
            )
        await db.commit()

    release = asyncio.Event()
    resumed = []

    async def fake_run_login(owner_id, phone, **kwargs):
        resumed.append(owner_id)
        await release.wait()

    monkeypatch.setattr(tenants, "_run_login", fake_run_login)

    await tenants.resume_all_tenants()
    # ждём только того, у кого уже была привязана группа (active); тем, кто ещё
    # ждёт код или группу, мост принимать пока нечего
    assert state.tenants_resuming == {1}

    await asyncio.sleep(0.05)
    assert sorted(resumed) == [1, 2, 3]  # переподключаются все, кроме failed
    release.set()
    await asyncio.sleep(0.05)
    assert state.tenants_resuming == set()


async def test_failed_login_still_stops_the_waiting(monkeypatch):
    async def failing_login(owner_id, phone, **kwargs):
        raise RuntimeError("MAX недоступен")

    monkeypatch.setattr(tenants, "_run_login", failing_login)
    state.tenants_resuming.add(5)
    with pytest.raises(RuntimeError):
        await tenants._resume_one(5, "7900")
    assert 5 not in state.tenants_resuming
