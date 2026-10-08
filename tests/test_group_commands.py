import pytest

import relay_tg_to_max as r
import state
import tenants


class FakeChat:
    id = -1005550001
    is_forum = True
    type = "supergroup"


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.chat = FakeChat()
        self.media_group_id = None
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


@pytest.mark.parametrize("text", ["/logout", "/cancel", "/logout@some_bot", " /cancel  ", "/logout сейчас"])
def test_private_only_commands_detected(text):
    assert r._is_private_only_command(text)


@pytest.mark.parametrize("text", ["привет", "/info", "/start", "логаут /logout", "/logouts", "", None])
def test_other_text_is_not_private_only_command(text):
    assert not r._is_private_only_command(text)


@pytest.mark.parametrize("text", ["/logout", "/cancel"])
async def test_private_command_in_bridged_group_gets_hint_and_is_not_forwarded(monkeypatch, text):
    monkeypatch.setattr(r, "_resolve_route", lambda chat_id: (object(), "unused.db"))
    msg = FakeMessage(text)

    await r.telegram_to_max(msg)  # при пересылке упал бы на отсутствующих атрибутах

    assert msg.replies == ["Эту команду нужно писать боту в личные сообщения, а не в группе."]


async def test_group_setup_instructions_explain_topics_before_admin_rights(monkeypatch):
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append(text)

    monkeypatch.setattr(state, "tg_bot", FakeBot())
    await tenants._send_group_setup_instructions(123)

    text = sent[0]
    assert "Управление темами" in text
    assert "после того, как включишь «Темы»" in text
    assert "/setupgroup" in text
    assert "не мне в личку" not in text
