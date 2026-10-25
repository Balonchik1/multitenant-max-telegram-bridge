import asyncio

import pytest

import relay_tg_to_max as r
import state
import tenants

GROUP = -1009990777
OWNER = 777009
THREAD = 87

WEBPAGE_CURL = 'Telegram server says - Bad Request: failed to send message #1 with the error message "WEBPAGE_CURL_FAILED"'
NO_RIGHTS = "Telegram server says - Bad Request: not enough rights to send text messages to the chat"


@pytest.mark.parametrize("text", [
    WEBPAGE_CURL,
    'Bad Request: failed to send message #2 with the error message "WEBPAGE_MEDIA_EMPTY"',
    "Bad Request: failed to get HTTP URL content",
    'Bad Request: failed to send message #1 with the error message "Wrong file identifier/HTTP URL specified"',
    "Bad Request: file is too big",
])
def test_broken_file_or_link_is_not_a_group_problem(text):
    assert r._is_content_send_error(Exception(text))


@pytest.mark.parametrize("text", [
    NO_RIGHTS,
    "Forbidden: bot was kicked from the supergroup chat",
    "Bad Request: chat not found",
    "Bad Request: TOPIC_CLOSED",
])
def test_real_group_problems_are_still_group_problems(text):
    assert not r._is_content_send_error(Exception(text))


class FailingBot:
    """Основная отправка падает с заданной ошибкой; заметки в тему проходят."""

    def __init__(self, error):
        self.error = error
        self.notes = []

    async def send_message(self, chat_id, text=None, **kwargs):
        if text == "видео из MAX":
            raise Exception(self.error)
        self.notes.append((chat_id, text, kwargs.get("message_thread_id")))

        class _Sent:
            message_id = 1

        return _Sent()


async def _run_failing_task(monkeypatch, error):
    alarms = []

    async def fake_notify(owner_id, group_id, reason):
        alarms.append((owner_id, reason))

    bot = FailingBot(error)
    monkeypatch.setattr(tenants, "notify_group_broken", fake_notify)
    monkeypatch.setattr(state, "tg_bot", bot)
    monkeypatch.setitem(state.tenant_group_map, GROUP, OWNER)

    state.tg_queue = asyncio.Queue()
    worker = asyncio.create_task(r.tg_worker())
    await state.tg_queue.put({
        "method": "send_message",
        "kwargs": {"chat_id": GROUP, "text": "видео из MAX", "message_thread_id": THREAD},
        "max_chat_id": -500,
        "max_message_id": 1,
    })
    await asyncio.sleep(5)  # три попытки с паузами 1.5 с и 3 с
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    return alarms, bot


async def test_unfetchable_media_link_does_not_send_broken_bridge_alarm(monkeypatch):
    """Живой случай (12 и 13 октября): видео из MAX, которое Telegram не смог
    скачать по ссылке, поднимало подруге чек-лист «мост перестал работать»."""
    alarms, bot = await _run_failing_task(monkeypatch, WEBPAGE_CURL)

    assert alarms == [], f"чек-лист про права ни к чему: {alarms}"
    assert any(
        text and "проблема с файлом или ссылкой" in text and thread == THREAD
        for _, text, thread in bot.notes
    ), "в тему должна уйти короткая заметка про файл"


async def test_real_permission_problem_still_raises_the_alarm(monkeypatch):
    alarms, _ = await _run_failing_task(monkeypatch, NO_RIGHTS)

    assert len(alarms) == 1 and alarms[0][0] == OWNER
    assert "не смог отправить сообщение" in alarms[0][1]
