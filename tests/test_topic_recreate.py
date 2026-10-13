import asyncio
import sqlite3

import pytest

import relay_tg_to_max as r
import state


class FakeSent:
    class chat:
        id = 1

    message_id = 11
    message_thread_id = None


@pytest.fixture
async def dialogs_db(tmp_path):
    path = str(tmp_path / "t.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE dialogs (max_chat_id INTEGER PRIMARY KEY, telegram_chat_id INTEGER, telegram_thread_id INTEGER, "
        "topic_name TEXT, max_sender_id INTEGER)"
    )
    con.execute("INSERT INTO dialogs VALUES (-500, 1, 87, 'old', NULL)")
    con.commit()
    con.close()
    return path


async def test_concurrent_tasks_share_one_recreated_topic(monkeypatch, dialogs_db):
    """Живой случай: добор пропущенного клал в очередь несколько сообщений с
    одним и тем же мёртвым thread_id, и каждое заводило свою новую тему."""
    created = []

    async def fake_do_recreate(tg_group_id, max_chat_id, db_path, max_sender_id=None):
        await asyncio.sleep(0.01)
        new_id = 100 + len(created)
        created.append(new_id)
        con = sqlite3.connect(db_path)
        con.execute("UPDATE dialogs SET telegram_thread_id = ? WHERE max_chat_id = ?", (new_id, max_chat_id))
        con.commit()
        con.close()
        return new_id

    monkeypatch.setattr(r, "_do_recreate_topic", fake_do_recreate)
    r._recreate_locks.clear()

    results = await asyncio.gather(*[
        r._recreate_topic(1, -500, dialogs_db, None, failed_thread_id=87) for _ in range(5)
    ])

    assert len(created) == 1, f"создано {len(created)} тем вместо одной"
    assert results == [100] * 5


async def test_topic_is_recreated_when_stored_thread_is_the_failed_one(monkeypatch, dialogs_db):
    calls = []

    async def fake_do_recreate(tg_group_id, max_chat_id, db_path, max_sender_id=None):
        calls.append(1)
        return 555

    monkeypatch.setattr(r, "_do_recreate_topic", fake_do_recreate)
    r._recreate_locks.clear()

    assert await r._recreate_topic(1, -500, dialogs_db, None, failed_thread_id=87) == 555
    assert calls == [1]


async def test_recreate_without_known_failed_thread_always_creates(monkeypatch, dialogs_db):
    async def fake_do_recreate(tg_group_id, max_chat_id, db_path, max_sender_id=None):
        return 777

    monkeypatch.setattr(r, "_do_recreate_topic", fake_do_recreate)
    r._recreate_locks.clear()

    assert await r._recreate_topic(1, -500, dialogs_db, None) == 777


class DeadThreadBot:
    """Тема удалена; создать новую не получается (flood control)."""

    def __init__(self):
        self.sent_to_owner = []

    async def send_message(self, chat_id, text=None, **kwargs):
        if kwargs.get("message_thread_id") is not None:
            raise Exception("Telegram server says - Bad Request: message thread not found")
        self.sent_to_owner.append((chat_id, text))
        return FakeSent()


async def test_missing_thread_does_not_raise_broken_bridge_alarm(monkeypatch):
    import tenants

    alarms = []

    async def fake_notify(owner_id, group_id, reason):
        alarms.append(reason)

    async def fake_recreate(*args, **kwargs):
        return None  # пересоздать тему не удалось

    monkeypatch.setattr(tenants, "notify_group_broken", fake_notify)
    monkeypatch.setattr(r, "_recreate_topic", fake_recreate)
    monkeypatch.setattr(state, "tg_bot", DeadThreadBot())
    monkeypatch.setitem(state.tenant_group_map, -1009990, 4242)

    state.tg_queue = asyncio.Queue()
    worker = asyncio.create_task(r.tg_worker())
    await state.tg_queue.put({
        "method": "send_message",
        "kwargs": {"chat_id": -1009990, "text": "hi", "message_thread_id": 87},
        "max_chat_id": -500,
        "max_message_id": 1,
    })
    await asyncio.sleep(5)  # три попытки с паузами 1.5 с и 3 с
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    assert alarms == [], f"тревога про права бота ни к чему: {alarms}"
