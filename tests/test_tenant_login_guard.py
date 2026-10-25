import asyncio
import os
import sqlite3
import types

import aiosqlite
import pytest

import login_guard
import relay_max_to_tg
import state
import tenants

OWNER = 777002
GROUP = -1009990002


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    def to(self, chat_id):
        return [text for cid, text in self.sent if cid == chat_id]


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", str(tmp_path / "bridge.db"))
    monkeypatch.setattr(state, "TENANTS_ROOT", str(tmp_path / "tenants"))
    bot = FakeBot()
    monkeypatch.setattr(state, "tg_bot", bot)
    await tenants.init_tenants_table()
    tenant_dir = os.path.dirname(state.tenant_db_path(OWNER))
    os.makedirs(tenant_dir)
    sqlite3.connect(state.tenant_db_path(OWNER)).close()
    yield {"bot": bot, "cache": state.tenant_cache_dir(OWNER)}
    state.tenant_clients.pop(OWNER, None)
    state.tenant_group_map.pop(GROUP, None)
    tenants._run_tasks.pop(OWNER, None)


async def _set_tenant(status, group=GROUP):
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute("DELETE FROM tenant_accounts WHERE owner_id = ?", (OWNER,))
        await db.execute(
            "INSERT INTO tenant_accounts (owner_id, max_phone, status, tg_group_id) VALUES (?, '79990000000', ?, ?)",
            (OWNER, status, group),
        )
        await db.commit()


async def _status():
    return (await tenants._get_tenant(OWNER))["status"]


# --- сообщение пользователю, а не админу, и никаких SMS ---

async def test_lost_login_tells_the_user_to_retry_and_informs_admin_once(env):
    await _set_tenant("active")
    state.tenant_clients[OWNER] = object()

    await tenants._handle_login_outcome_failure(OWNER, login_guard.LoginNotAllowed("x"))

    assert await _status() == "failed"
    assert OWNER not in state.tenant_clients
    to_user = env["bot"].to(OWNER)
    assert len(to_user) == 1 and "/retry" in to_user[0] and "MAX завершил вход" in to_user[0]
    assert "LoginNotAllowed" not in to_user[0] and "вход слетел" not in to_user[0]  # без служебных слов
    to_admin = env["bot"].to(tenants.ADMIN_ID)
    assert len(to_admin) == 1 and str(OWNER) in to_admin[0]


async def test_revoked_session_while_running_is_reported_the_same_way(env):
    await _set_tenant("active")

    async def revoked():
        raise login_guard.LoginNotAllowed("x")

    task = asyncio.ensure_future(revoked())
    tenants._run_tasks[OWNER] = task
    await tenants._watch_tenant_task(OWNER, task)

    assert await _status() == "failed"
    assert len(env["bot"].to(OWNER)) == 1 and "/retry" in env["bot"].to(OWNER)[0]


async def test_other_connection_errors_still_get_the_old_message(env):
    await _set_tenant("active")

    async def broken():
        raise ConnectionError("Not connected")

    task = asyncio.ensure_future(broken())
    tenants._run_tasks[OWNER] = task
    await tenants._watch_tenant_task(OWNER, task)

    assert "Соединение с MAX прервалось" in env["bot"].to(OWNER)[0]
    assert await _status() == "active"  # обычный обрыв статус не меняет


# --- разрешение на SMS: только когда человек сам начал вход ---

async def test_resume_after_restart_never_asks_for_sms(monkeypatch):
    seen = {}

    async def fake_run_login(owner_id, phone, allow_sms=True):
        seen["allow_sms"] = allow_sms

    monkeypatch.setattr(tenants, "_run_login", fake_run_login)
    await tenants._resume_one(OWNER, "79990000000")
    assert seen == {"allow_sms": False}


class HangingClient:
    """pymax-клиент, который запомнил, с каким предохранителем его создали."""

    instances = []

    def __init__(self, phone=None, work_dir=None, session_name=None, auth_flow=None):
        self.auth_flow = auth_flow
        self.closed = False
        self._stop = asyncio.Event()
        self.extra_config = types.SimpleNamespace(reconnect=True, relogin=True)
        HangingClient.instances.append(self)

    def on_start(self):
        return lambda fn: fn

    async def start(self):
        await self._stop.wait()
        raise RuntimeError("клиент закрыт")

    async def close(self):
        self.closed = True
        self._stop.set()


async def _start_login(allow_sms):
    HangingClient.instances.clear()
    task = asyncio.create_task(tenants._run_login(OWNER, "79990000000", allow_sms=allow_sms))
    for _ in range(100):
        if OWNER in state.tenant_clients:
            break
        await asyncio.sleep(0.02)
    assert OWNER in state.tenant_clients
    return task


async def _finish(task):
    await tenants.perform_logout(OWNER)
    await asyncio.wait_for(task, timeout=3)


async def test_login_started_by_the_user_gets_one_sms_permission(env, monkeypatch):
    monkeypatch.setattr(tenants, "Client", HangingClient)
    await _set_tenant("awaiting_login", group=None)
    login_guard.mark_login_ok(env["cache"])  # вход раньше удавался, значит без разрешения SMS нельзя

    task = await _start_login(allow_sms=True)
    assert os.path.exists(os.path.join(env["cache"], "allow_sms.flag"))
    flow = HangingClient.instances[0].auth_flow
    assert isinstance(flow, login_guard.GuardedSmsAuthFlow) and flow.cache_dir == env["cache"]
    await _finish(task)


async def test_silent_reconnect_gets_no_sms_permission(env, monkeypatch):
    monkeypatch.setattr(tenants, "Client", HangingClient)
    await _set_tenant("active")
    login_guard.mark_login_ok(env["cache"])

    task = await _start_login(allow_sms=False)
    assert not os.path.exists(os.path.join(env["cache"], "allow_sms.flag"))
    await _finish(task)


# --- после /retry мост восстанавливается без повторной настройки группы ---

class LoggingInClient(HangingClient):
    def __init__(self, phone=None, work_dir=None, session_name=None, auth_flow=None):
        super().__init__(phone, work_dir, session_name, auth_flow)
        self._handlers = []
        self.me = types.SimpleNamespace(contact=types.SimpleNamespace(id=555))

    def on_start(self):
        def decorator(fn):
            self._handlers.append(fn)
            return fn
        return decorator

    async def start(self):
        for handler in self._handlers:
            await handler(self)
        await self._stop.wait()
        raise RuntimeError("клиент закрыт")


@pytest.fixture
def restore_env(env, monkeypatch):
    calls = {"register": [], "backfill": []}
    monkeypatch.setattr(tenants, "Client", LoggingInClient)
    monkeypatch.setattr(
        relay_max_to_tg, "register_tenant_handlers",
        lambda client, *, tg_group_id, db_path: calls["register"].append(tg_group_id),
    )

    async def fake_backfill(client, *, tg_group_id, db_path):
        calls["backfill"].append(tg_group_id)

    monkeypatch.setattr(relay_max_to_tg, "_backfill_missed_messages", fake_backfill)
    yield calls
    client = state.tenant_clients.pop(OWNER, None)
    tenants._run_tasks.pop(OWNER, None)
    if client is not None:
        client._stop.set()


async def test_retry_after_lost_login_restores_the_bridge_without_group_setup(env, restore_env):
    await _set_tenant("awaiting_login")  # так /retry переводит запись из «failed»

    await asyncio.wait_for(tenants._run_login(OWNER, "79990000000"), timeout=5)

    assert await _status() == "active"
    assert state.tenant_group_map[GROUP] == OWNER
    assert restore_env["register"] == [GROUP] and restore_env["backfill"] == [GROUP]
    texts = env["bot"].to(OWNER)
    assert len(texts) == 1 and "мост снова работает" in texts[0]
    assert "создай" not in texts[0].lower()


async def test_quiet_reconnect_of_an_active_user_stays_quiet(env, restore_env):
    await _set_tenant("active")

    await asyncio.wait_for(tenants._run_login(OWNER, "79990000000", allow_sms=False), timeout=5)

    assert env["bot"].to(OWNER) == []
    assert await _status() == "active"


async def test_new_user_without_a_group_still_gets_the_setup_instructions(env, restore_env):
    await _set_tenant("awaiting_login", group=None)

    await asyncio.wait_for(tenants._run_login(OWNER, "79990000000"), timeout=5)

    assert await _status() == "awaiting_group"
    assert restore_env["register"] == []
    assert any("Теперь создай отдельную группу" in t for t in env["bot"].to(OWNER))
