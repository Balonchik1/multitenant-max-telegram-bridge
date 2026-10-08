import aiosqlite
import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import SkipHandler

import access
import config
import tenants

ADMIN = 424242
USER = 555001


class FakeUser:
    def __init__(self, uid, username="someone", first="Тест", last=""):
        self.id = uid
        self.username = username
        self.first_name = first
        self.last_name = last


class FakeMessage:
    def __init__(self, uid, text="/start"):
        self.from_user = FakeUser(uid)
        self.text = text
        self.chat = type("C", (), {"type": "private"})()
        self.replies = []
        self.markups = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


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


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bridge.db")
    await access.init_access_table(db_path)
    bot = FakeBot()
    dp = Dispatcher()
    access.setup_access(dp, bot, db_path, ADMIN)

    onboarded = []

    async def fake_start_onboarding(uid):
        onboarded.append(uid)

    monkeypatch.setattr(tenants, "start_onboarding", fake_start_onboarding)

    async def fake_revoke_nothing(uid):
        return None

    monkeypatch.setattr(tenants, "revoke_tenant", fake_revoke_nothing)

    handlers = {h.callback.__name__: h.callback for h in dp.message.handlers}
    handlers.update({h.callback.__name__: h.callback for h in dp.callback_query.handlers})
    return {"bot": bot, "handlers": handlers, "onboarded": onboarded}


async def _status(uid):
    return await access.get_access_status(uid)


def _admin_notifications(env):
    return [(chat, text, kw) for chat, text, kw in env["bot"].sent if chat == ADMIN]


async def test_first_start_shows_intro_and_does_not_notify_admin(env, monkeypatch):
    """Человек, который нажал Start вслепую, сначала видит объяснение, а заявка
    (и уведомление админу) появляется только после его осознанного нажатия."""
    monkeypatch.setattr(access, "REQUIRE_APPROVAL", True)
    msg = FakeMessage(USER)
    await env["handlers"]["cmd_start"](msg)

    assert await _status(USER) is None
    assert env["onboarded"] == []
    assert _admin_notifications(env) == []
    assert "полный доступ" in msg.replies[0]
    assert "/logout" in msg.replies[0]
    assert msg.markups[0].inline_keyboard[0][0].callback_data == f"access:request:{USER}"


async def test_request_button_creates_pending_request_with_approval_buttons(env, monkeypatch):
    monkeypatch.setattr(access, "REQUIRE_APPROVAL", True)
    q = FakeQuery(USER, f"access:request:{USER}")
    await env["handlers"]["cb_access"](q)

    assert await _status(USER) == "pending"
    assert env["onboarded"] == []
    assert any("Заявка отправлена" in e for e in q.edits)
    notes = _admin_notifications(env)
    assert len(notes) == 1
    markup = notes[0][2]["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == [f"access:allow:{USER}", f"access:deny:{USER}"]


async def test_open_registration_still_works_when_approval_disabled(env, monkeypatch):
    monkeypatch.setattr(access, "REQUIRE_APPROVAL", False)
    msg = FakeMessage(USER)
    await env["handlers"]["cmd_start"](msg)

    assert await _status(USER) == "allowed"
    assert env["onboarded"] == [USER]


async def test_pending_user_start_again_does_not_spam_admin(env, monkeypatch):
    monkeypatch.setattr(access, "REQUIRE_APPROVAL", True)
    await env["handlers"]["cb_access"](FakeQuery(USER, f"access:request:{USER}"))
    msg = FakeMessage(USER)
    await env["handlers"]["cmd_start"](msg)

    assert "уже на рассмотрении" in msg.replies[0]
    assert len(_admin_notifications(env)) == 1


async def test_request_button_pressed_twice_sends_one_request(env, monkeypatch):
    monkeypatch.setattr(access, "REQUIRE_APPROVAL", True)
    await env["handlers"]["cb_access"](FakeQuery(USER, f"access:request:{USER}"))
    q2 = FakeQuery(USER, f"access:request:{USER}")
    await env["handlers"]["cb_access"](q2)

    assert q2.answers == [("Заявка уже на рассмотрении", False)]
    assert len(_admin_notifications(env)) == 1


async def test_request_button_of_another_user_is_rejected(env, monkeypatch):
    monkeypatch.setattr(access, "REQUIRE_APPROVAL", True)
    q = FakeQuery(999, f"access:request:{USER}")
    await env["handlers"]["cb_access"](q)

    assert q.answers == [("Это не твоя кнопка", True)]
    assert await _status(USER) is None
    assert _admin_notifications(env) == []


async def test_first_message_without_start_shows_intro_not_request(env, monkeypatch):
    monkeypatch.setattr(access, "REQUIRE_APPROVAL", True)
    msg = FakeMessage(USER, text="привет")
    await env["handlers"]["private_non_admin"](msg)

    assert await _status(USER) is None
    assert env["onboarded"] == []
    assert _admin_notifications(env) == []
    assert "полный доступ" in msg.replies[0]


async def test_pending_user_can_still_use_support_but_not_chat(env, monkeypatch):
    monkeypatch.setattr(access, "REQUIRE_APPROVAL", True)
    await access.upsert_access(USER, "pending")

    with pytest.raises(SkipHandler):
        await env["handlers"]["private_non_admin"](FakeMessage(USER, text="/support нужен доступ"))

    msg = FakeMessage(USER, text="просто сообщение")
    await env["handlers"]["private_non_admin"](msg)
    assert "на рассмотрении" in msg.replies[0]


async def test_admin_allow_button_approves_and_starts_onboarding(env):
    await access.upsert_access(USER, "pending")
    q = FakeQuery(ADMIN, f"access:allow:{USER}")
    await env["handlers"]["cb_access"](q)

    assert await _status(USER) == "allowed"
    assert env["onboarded"] == [USER]
    assert any(chat == USER and "одобрил" in text for chat, text, _ in env["bot"].sent)
    assert any("Разрешён" in e for e in q.edits)


async def test_admin_deny_button_rejects_and_notifies_user(env):
    await access.upsert_access(USER, "pending")
    q = FakeQuery(ADMIN, f"access:deny:{USER}")
    await env["handlers"]["cb_access"](q)

    assert await _status(USER) == "denied"
    assert env["onboarded"] == []
    assert any(chat == USER and "отклонена" in text for chat, text, _ in env["bot"].sent)


async def test_deny_button_disconnects_connected_user_and_skips_generic_refusal(env, monkeypatch):
    async def fake_revoke(uid):
        return {"server_logout": True, "was_logged_in": True, "tg_group_id": None}

    monkeypatch.setattr(tenants, "revoke_tenant", fake_revoke)
    await access.upsert_access(USER, "allowed")
    q = FakeQuery(ADMIN, f"access:deny:{USER}")
    await env["handlers"]["cb_access"](q)

    assert await _status(USER) == "denied"
    assert any("сессия в MAX завершена" in e for e in q.edits), q.edits
    assert not any(chat == USER for chat, _, _ in env["bot"].sent), "общий отказ лишний: человека уже предупредил revoke_tenant"


async def test_deny_command_disconnects_connected_user(env, monkeypatch):
    async def fake_revoke(uid):
        return {"server_logout": False, "was_logged_in": True, "tg_group_id": None}

    monkeypatch.setattr(tenants, "revoke_tenant", fake_revoke)
    msg = FakeMessage(ADMIN, text=f"/deny {USER}")
    await env["handlers"]["cmd_deny"](msg)

    assert await _status(USER) == "denied"
    assert "не подтвердил" in msg.replies[0]
    assert not any(chat == USER for chat, _, _ in env["bot"].sent)


async def test_deny_command_for_not_connected_user_keeps_generic_refusal(env, monkeypatch):
    async def fake_revoke(uid):
        return None

    monkeypatch.setattr(tenants, "revoke_tenant", fake_revoke)
    msg = FakeMessage(ADMIN, text=f"/deny {USER}")
    await env["handlers"]["cmd_deny"](msg)

    assert await _status(USER) == "denied"
    assert any(chat == USER and "в разработке" in text for chat, text, _ in env["bot"].sent)


async def test_non_admin_cannot_press_approval_buttons(env):
    await access.upsert_access(USER, "pending")
    q = FakeQuery(999, f"access:allow:{USER}")
    await env["handlers"]["cb_access"](q)

    assert q.answers == [("Только админ может решать", True)]
    assert await _status(USER) == "pending"
    assert env["onboarded"] == []


def test_env_flag_parsing(monkeypatch):
    monkeypatch.delenv("X_FLAG", raising=False)
    assert config._env_flag("X_FLAG", default=True) is True
    assert config._env_flag("X_FLAG", default=False) is False
    for off in ("false", "FALSE", "0", "no", "off", " Off "):
        monkeypatch.setenv("X_FLAG", off)
        assert config._env_flag("X_FLAG", default=True) is False, off
    for on in ("true", "1", "yes", "on"):
        monkeypatch.setenv("X_FLAG", on)
        assert config._env_flag("X_FLAG", default=False) is True, on
    monkeypatch.setenv("X_FLAG", "")
    assert config._env_flag("X_FLAG", default=True) is True
