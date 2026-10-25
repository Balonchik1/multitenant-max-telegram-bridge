import asyncio
import os

import pytest
from pymax import ConsoleSmsCodeProvider, SmsAuthFlow

import bridge
import commands
import login_guard
import state


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(login_guard, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(state, "admin_login_blocked", False)
    return tmp_path


# --- сам предохранитель ---

def test_first_install_may_request_sms():
    assert login_guard.sms_allowed() is True
    assert login_guard.sms_allowed() is True  # без метки разрешение не расходуется


def test_after_a_successful_login_sms_is_not_requested_on_its_own():
    login_guard.mark_login_ok()
    assert login_guard.sms_allowed() is False
    assert login_guard.sms_allowed() is False


def test_permission_is_one_shot():
    login_guard.mark_login_ok()
    login_guard.grant_permission()
    assert login_guard.sms_allowed() is True
    assert login_guard.sms_allowed() is False  # второго SMS уже не будет


def test_successful_login_drops_a_stale_permission():
    login_guard.mark_login_ok()
    login_guard.grant_permission()
    login_guard.mark_login_ok()  # вход удался без того, чтобы разрешение понадобилось
    assert login_guard.sms_allowed() is False


async def test_flow_refuses_without_permission_and_never_requests_sms(monkeypatch):
    called = []

    async def fake_authenticate(self, app):
        called.append(1)
        return "token-result"

    monkeypatch.setattr(SmsAuthFlow, "authenticate", fake_authenticate)
    login_guard.mark_login_ok()
    flow = login_guard.GuardedSmsAuthFlow(ConsoleSmsCodeProvider())

    with pytest.raises(login_guard.LoginNotAllowed):
        await flow.authenticate(object())
    assert called == []  # заказ SMS даже не начинался


async def test_flow_runs_the_normal_login_once_when_permitted(monkeypatch):
    async def fake_authenticate(self, app):
        return "token-result"

    monkeypatch.setattr(SmsAuthFlow, "authenticate", fake_authenticate)
    login_guard.mark_login_ok()
    login_guard.grant_permission()
    flow = login_guard.GuardedSmsAuthFlow(ConsoleSmsCodeProvider())

    assert await flow.authenticate(object()) == "token-result"
    with pytest.raises(login_guard.LoginNotAllowed):
        await flow.authenticate(object())  # повтор после таймаута кода: уже нельзя


def test_main_client_uses_the_guarded_flow():
    assert isinstance(state.max_client._auth_flow, login_guard.GuardedSmsAuthFlow)


# --- запуск основного клиента ---

class FakeMaxClient:
    def __init__(self, error=None):
        self.error = error
        self.started = 0

    async def start(self):
        self.started += 1
        if self.error:
            raise self.error


async def test_blocked_login_notifies_once_and_keeps_the_process_alive(monkeypatch):
    notified = []

    async def fake_notify():
        notified.append(1)

    monkeypatch.setattr(state, "max_client", FakeMaxClient(login_guard.LoginNotAllowed("x")))
    monkeypatch.setattr(commands, "notify_login_needed", fake_notify)
    admin_ready = asyncio.Event()

    task = asyncio.create_task(bridge.run_admin_client(admin_ready))
    await asyncio.wait_for(admin_ready.wait(), timeout=2)
    await asyncio.sleep(0.05)

    assert notified == [1]
    assert state.admin_login_blocked is True
    assert not task.done(), "задача не должна завершаться: иначе бот уйдёт в перезапуск и отключит подключённых пользователей"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_other_start_errors_still_stop_the_bridge(monkeypatch):
    monkeypatch.setattr(state, "max_client", FakeMaxClient(RuntimeError("chats.outage")))
    with pytest.raises(RuntimeError):
        await bridge.run_admin_client(asyncio.Event())


async def test_normal_start_just_returns(monkeypatch):
    client = FakeMaxClient()
    monkeypatch.setattr(state, "max_client", client)
    await bridge.run_admin_client(asyncio.Event())
    assert client.started == 1
    assert state.admin_login_blocked is False


# --- команда /relogin ---

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, kind):
        self.type = kind


class FakeMessage:
    def __init__(self, uid, text="/relogin", kind="private"):
        self.from_user = FakeUser(uid)
        self.text = text
        self.chat = FakeChat(kind)
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


@pytest.fixture
def restarts(monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "_restart_process_soon", lambda delay=2.0: calls.append(delay))
    return calls


def test_relogin_command_is_recognised_only_in_private():
    assert commands._is_relogin_command(FakeMessage(1, "/relogin"))
    assert commands._is_relogin_command(FakeMessage(1, "/relogin@some_bot"))
    assert not commands._is_relogin_command(FakeMessage(1, "/relogin", kind="supergroup"))
    assert not commands._is_relogin_command(FakeMessage(1, "/relogins"))


async def test_relogin_grants_one_sms_and_restarts_when_login_is_lost(monkeypatch, restarts, cache_dir):
    login_guard.mark_login_ok()
    monkeypatch.setattr(state, "admin_login_blocked", True)
    monkeypatch.setattr(commands, "_main_client_connected", lambda: False)
    msg = FakeMessage(commands.ADMIN_ID)

    await commands.cmd_relogin(msg)

    assert login_guard.sms_allowed() is True and login_guard.sms_allowed() is False
    assert restarts == [2.0] or len(restarts) == 1
    assert "один SMS-код" in msg.replies[0]


async def test_relogin_does_nothing_when_account_is_already_connected(monkeypatch, restarts):
    login_guard.mark_login_ok()
    monkeypatch.setattr(commands, "_main_client_connected", lambda: True)
    msg = FakeMessage(commands.ADMIN_ID)

    await commands.cmd_relogin(msg)

    assert restarts == []
    assert login_guard.sms_allowed() is False  # разрешение не выдано
    assert "уже подключён" in msg.replies[0]


async def test_relogin_ignores_everyone_but_the_admin(monkeypatch, restarts):
    login_guard.mark_login_ok()
    monkeypatch.setattr(state, "admin_login_blocked", True)
    monkeypatch.setattr(commands, "_main_client_connected", lambda: False)
    msg = FakeMessage(commands.ADMIN_ID + 1)

    await commands.cmd_relogin(msg)

    assert restarts == [] and msg.replies == []
    assert login_guard.sms_allowed() is False
