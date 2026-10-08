import asyncio
import os
import sqlite3
import types

import aiosqlite
import pytest

import relay_max_to_tg
import state
import tenants


class FakeClient:
    def __init__(self, logout_error=None):
        self.logout_error = logout_error
        self.logout_called = False
        self.closed = False
        self.extra_config = types.SimpleNamespace(reconnect=True, relogin=True)

    async def logout(self):
        self.logout_called = True
        if self.logout_error:
            raise self.logout_error
        return True

    async def close(self):
        self.closed = True


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, uid, text="/logout"):
        self.from_user = FakeUser(uid)
        self.text = text
        self.chat = type("C", (), {"type": "private"})()
        self.replies = []
        self.markups = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FakeQuery:
    def __init__(self, uid, data):
        self.from_user = FakeUser(uid)
        self.data = data
        self.answers = []
        self.edits = []
        outer = self

        class _Msg:
            async def edit_text(self, text, **kwargs):
                outer.edits.append(text)

        self.message = _Msg()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


OWNER = 777001
GROUP = -1009990001


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", str(tmp_path / "bridge.db"))
    monkeypatch.setattr(state, "TENANTS_ROOT", str(tmp_path / "tenants"))
    bot = FakeBot()
    monkeypatch.setattr(state, "tg_bot", bot)
    monkeypatch.setattr(tenants, "_logging_out", set())
    await tenants.init_tenants_table()

    tenant_dir = os.path.dirname(state.tenant_db_path(OWNER))
    os.makedirs(os.path.join(tenant_dir, "cache"))
    with open(os.path.join(tenant_dir, "cache", "main.db"), "w") as f:
        f.write("session")
    sqlite3.connect(state.tenant_db_path(OWNER)).close()

    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            "INSERT INTO tenant_accounts (owner_id, max_phone, status, tg_group_id, max_2fa_password) "
            "VALUES (?, '79990000000', 'active', ?, 'secret')",
            (OWNER, GROUP),
        )
        await db.commit()

    state.tenant_group_map[GROUP] = OWNER
    state.tenant_2fa_passwords[OWNER] = "secret"
    yield {"bot": bot, "dir": tenant_dir}
    state.tenant_clients.pop(OWNER, None)
    state.tenant_group_map.pop(GROUP, None)
    state.tenant_2fa_passwords.pop(OWNER, None)
    tenants._run_tasks.pop(OWNER, None)


async def _row():
    return await tenants._get_tenant(OWNER)


async def test_logout_with_live_client_wipes_everything(env):
    client = FakeClient()
    state.tenant_clients[OWNER] = client
    task = asyncio.create_task(asyncio.sleep(3600))
    tenants._run_tasks[OWNER] = task

    result = await tenants.perform_logout(OWNER)

    assert result == {"server_logout": True, "tg_group_id": GROUP, "was_logged_in": True}
    assert client.logout_called and client.closed
    assert client._bridge_stopped is True
    # без этого pymax после рывка соединения переподключится и запросит SMS
    assert client.extra_config.reconnect is False
    assert client.extra_config.relogin is False
    assert task.cancelled()
    assert OWNER not in state.tenant_clients
    assert OWNER not in tenants._run_tasks
    assert GROUP not in state.tenant_group_map
    assert OWNER not in state.tenant_2fa_passwords
    assert not os.path.exists(env["dir"])

    row = await _row()
    assert row["status"] == "awaiting_phone"
    assert row["max_phone"] is None
    assert row["tg_group_id"] is None
    assert row["max_2fa_password"] is None


async def test_logout_failure_on_max_side_still_deletes_local_data(env):
    client = FakeClient(logout_error=RuntimeError("нет связи"))
    state.tenant_clients[OWNER] = client

    result = await tenants.perform_logout(OWNER)

    assert result["server_logout"] is False
    assert not os.path.exists(env["dir"])
    assert (await _row())["status"] == "awaiting_phone"


async def _stubborn_sleep():
    """Игнорирует первую отмену (как клиент pymax, который при cancel не
    завершается), потом всё же заканчивается — чтобы не оставлять хвостов."""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await asyncio.sleep(0.5)


class HangingClient(FakeClient):
    async def logout(self):
        await _stubborn_sleep()

    async def close(self):
        await _stubborn_sleep()


async def test_logout_does_not_hang_on_unresponsive_client(env, monkeypatch):
    """Живой случай: MAX рвёт соединение на запрос выхода, а клиент и его
    фоновая задача не реагируют на отмену — /logout висел на «Отключаю…»
    и данные не удалялись."""
    monkeypatch.setattr(tenants, "_LOGOUT_TIMEOUT", 0.1)
    state.tenant_clients[OWNER] = HangingClient()
    tenants._run_tasks[OWNER] = asyncio.create_task(_stubborn_sleep())

    result = await asyncio.wait_for(tenants.perform_logout(OWNER), timeout=3)

    assert result["server_logout"] is False
    assert not os.path.exists(env["dir"])
    assert (await _row())["status"] == "awaiting_phone"
    await asyncio.sleep(0.7)  # даём «упрямым» корутинам дозавершиться


class LoginInProgressClient(FakeClient):
    """Клиент, у которого start() не завершается, пока его не закроют, —
    как pymax, который ждёт SMS-код."""

    instances = []

    def __init__(self, phone=None, work_dir=None, session_name=None):
        super().__init__()
        self._stop = asyncio.Event()
        LoginInProgressClient.instances.append(self)

    def on_start(self):
        return lambda fn: fn

    async def start(self):
        await self._stop.wait()
        raise RuntimeError("клиент закрыт во время входа")

    async def close(self):
        self.closed = True
        self._stop.set()


async def test_logout_during_pending_login_does_not_report_failure(env, monkeypatch):
    """Живой случай: после рестарта MAX отозвал токен и pymax запросил SMS;
    /logout в это время не должен потом перетираться сообщением
    «Не получилось войти» и статусом failed."""
    LoginInProgressClient.instances.clear()
    monkeypatch.setattr(tenants, "Client", LoginInProgressClient)
    state.tenant_clients.pop(OWNER, None)

    login = asyncio.create_task(tenants._run_login(OWNER, "79990000000"))
    for _ in range(100):
        if OWNER in state.tenant_clients:
            break
        if login.done():
            login.result()  # покажет исключение, если вход упал раньше времени
        await asyncio.sleep(0.02)
    assert OWNER in state.tenant_clients

    await tenants.perform_logout(OWNER)
    await asyncio.wait_for(login, timeout=3)

    assert (await _row())["status"] == "awaiting_phone"
    assert all("Не получилось войти" not in t for _, t in env["bot"].sent), env["bot"].sent


async def test_logout_repeats_cancel_until_swallowing_task_stops(env, monkeypatch):
    """Живой случай: pymax глотает CancelledError внутри close(), одна отмена
    пропадала, цикл reconnect оживал и запрашивал SMS уже после /logout."""
    monkeypatch.setattr(tenants, "_CANCEL_STEP_TIMEOUT", 0.2)
    swallowed = []

    async def swallows_two_cancels():
        while len(swallowed) < 2:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                swallowed.append(1)

    state.tenant_clients[OWNER] = FakeClient()
    task = asyncio.create_task(swallows_two_cancels())
    tenants._run_tasks[OWNER] = task

    await asyncio.wait_for(tenants.perform_logout(OWNER), timeout=5)

    assert len(swallowed) == 2
    assert task.done()


async def test_logout_without_live_client_reports_unknown(env):
    result = await tenants.perform_logout(OWNER)

    assert result["server_logout"] is None
    assert not os.path.exists(env["dir"])
    assert (await _row())["status"] == "awaiting_phone"


async def test_revoke_tenant_disconnects_and_notifies_without_onboarding(env):
    """/deny для подключённого человека: отключаем как /logout, предупреждаем
    его и группу, но не запускаем онбординг заново."""
    state.tenant_clients[OWNER] = FakeClient()

    result = await tenants.revoke_tenant(OWNER)

    assert result["server_logout"] is True and result["was_logged_in"] is True
    assert not os.path.exists(env["dir"])
    assert (await _row())["status"] == "awaiting_phone"
    sent = env["bot"].sent
    assert any(chat == GROUP and "администратором" in text for chat, text in sent)
    assert any(chat == OWNER and "Администратор отключил" in text for chat, text in sent)
    assert not any("пришли номер телефона" in text for _, text in sent)


async def test_revoke_tenant_does_nothing_when_not_connected(env):
    await tenants._set_status(OWNER, "awaiting_phone")

    assert await tenants.revoke_tenant(OWNER) is None
    assert env["bot"].sent == []
    assert os.path.exists(env["dir"])


async def test_logout_command_asks_for_confirmation_when_active(env):
    msg = FakeMessage(OWNER)
    await tenants.cmd_logout(msg)

    assert "Отключить" in msg.replies[0]
    assert msg.markups[0] is not None
    assert (await _row())["status"] == "active"
    assert os.path.exists(env["dir"])


async def test_logout_command_nothing_to_disconnect_before_phone(env):
    await tenants._set_status(OWNER, "awaiting_phone")
    msg = FakeMessage(OWNER)
    await tenants.cmd_logout(msg)

    assert "Нечего отключать" in msg.replies[0]
    assert msg.markups[0] is None


async def test_logout_command_during_login_offers_to_cancel_it(env):
    """Во время входа /logout больше не отказывает: предлагает отменить вход
    (раньше человек застревал, пока бот ждал SMS-код)."""
    await tenants._set_status(OWNER, "awaiting_login")
    msg = FakeMessage(OWNER)
    await tenants.cmd_logout(msg)

    assert "Вход в MAX ещё не завершён" in msg.replies[0]
    assert msg.markups[0] is not None


async def test_cancel_aborts_pending_login_without_server_logout(env):
    await tenants._set_status(OWNER, "awaiting_login")
    client = FakeClient()
    state.tenant_clients[OWNER] = client

    msg = FakeMessage(OWNER, text="/cancel")
    await tenants.cmd_cancel(msg)

    assert not client.logout_called, "авторизованной сессии ещё нет — выход в MAX не нужен"
    assert any("Вход отменён" in r for r in msg.replies), msg.replies
    assert (await _row())["status"] == "awaiting_phone"
    assert not os.path.exists(env["dir"])
    assert any("пришли номер телефона" in t for chat, t in env["bot"].sent if chat == OWNER)


async def test_cancel_when_nothing_to_cancel(env):
    msg = FakeMessage(OWNER, text="/cancel")
    await tenants.cmd_cancel(msg)  # статус active

    assert "отменять нечего" in msg.replies[0]
    assert (await _row())["status"] == "active"
    assert os.path.exists(env["dir"])

    await tenants._set_status(OWNER, "awaiting_phone")
    msg2 = FakeMessage(OWNER, text="/cancel")
    await tenants.cmd_cancel(msg2)
    assert msg2.replies == ["Нечего отменять."]


async def test_cancel_pending_input_wakes_waiting_thread(monkeypatch):
    import auth_flow

    monkeypatch.setattr(auth_flow.state, "_main_loop", None)
    monkeypatch.setattr(auth_flow, "_auth_waiting", False)

    waiter = asyncio.create_task(asyncio.to_thread(auth_flow._tg_input, "Enter SMS code"))
    await asyncio.sleep(0.3)  # поток дошёл до блокирующего ожидания очереди
    auth_flow._auth_waiting = True  # как выставляет уведомление после отправки запроса

    assert auth_flow.cancel_pending_input() is True
    with pytest.raises(TimeoutError, match="отменён"):
        await asyncio.wait_for(waiter, timeout=3)
    assert auth_flow._auth_waiting is False


async def test_cancel_pending_input_does_nothing_when_not_waiting(monkeypatch):
    import auth_flow

    monkeypatch.setattr(auth_flow, "_auth_waiting", False)
    assert auth_flow.cancel_pending_input() is False
    assert auth_flow._auth_input_queue.empty()


async def test_logout_button_of_another_user_is_rejected(env):
    q = FakeQuery(uid=999, data=f"logout:yes:{OWNER}")
    await tenants.cb_logout(q)

    assert q.answers == [("Это не твоя кнопка", True)]
    assert (await _row())["status"] == "active"
    assert os.path.exists(env["dir"])


async def test_logout_cancel_changes_nothing(env):
    q = FakeQuery(uid=OWNER, data=f"logout:no:{OWNER}")
    await tenants.cb_logout(q)

    assert (await _row())["status"] == "active"
    assert os.path.exists(env["dir"])


async def test_logout_confirm_full_flow_notifies_and_restarts_onboarding(env):
    state.tenant_clients[OWNER] = FakeClient()
    q = FakeQuery(uid=OWNER, data=f"logout:yes:{OWNER}")

    await tenants.cb_logout(q)

    assert any("сессия в MAX завершена" in e for e in q.edits), q.edits
    sent_to = [chat for chat, _ in env["bot"].sent]
    assert GROUP in sent_to
    assert tenants.ADMIN_ID in sent_to
    onboarding = [text for chat, text in env["bot"].sent if chat == OWNER]
    assert any("пришли номер телефона" in t for t in onboarding), onboarding
    assert (await _row())["status"] == "awaiting_phone"
    assert OWNER not in tenants._logging_out


async def test_logout_without_max_confirmation_is_reported_calmly(env):
    """MAX всегда рвёт соединение на запрос выхода (подтверждения нет) — это
    нормальный исход, и текст не должен пугать предупреждением."""
    state.tenant_clients[OWNER] = FakeClient(logout_error=RuntimeError("Not connected to the server"))
    q = FakeQuery(uid=OWNER, data=f"logout:yes:{OWNER}")

    await tenants.cb_logout(q)

    final = q.edits[-1]
    assert "Готово" in final and "запрос на выход отправлен" in final
    assert "⚠️" not in final
    assert "не подтвердил" not in final


async def test_logout_without_live_client_still_warns(env):
    q = FakeQuery(uid=OWNER, data=f"logout:yes:{OWNER}")
    await tenants.cb_logout(q)

    assert "⚠️" in q.edits[-1]
    assert "не было живого соединения" in q.edits[-1]


async def test_watch_task_stays_silent_after_logout(env):
    async def boom():
        raise RuntimeError("соединение закрыто")

    task = asyncio.create_task(boom())
    # задача уже убрана из _run_tasks (как делает perform_logout)
    await tenants._watch_tenant_task(OWNER, task)

    assert env["bot"].sent == []


async def test_reaction_poll_loop_exits_when_client_stopped(monkeypatch):
    monkeypatch.setattr(relay_max_to_tg, "_REACTION_POLL_INTERVAL", 0.01)
    calls = []

    async def fake_poll(client, *, db_path):
        calls.append(1)

    monkeypatch.setattr(relay_max_to_tg, "_poll_reactions_once", fake_poll)

    client = FakeClient()
    client._bridge_stopped = True
    await asyncio.wait_for(relay_max_to_tg._reaction_poll_loop(client, db_path="x"), timeout=1)
    assert calls == []
