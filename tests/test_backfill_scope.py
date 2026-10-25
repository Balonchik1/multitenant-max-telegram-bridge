import time

import aiosqlite
import pytest

import db_messages
import relay_max_to_tg as rm

MAIN_CHAT = -101
FLAT_CHAT = -102
OTHER_GROUP_CHAT = -103


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "t.db")
    await db_messages.init_db(db_path=path)
    return path


class FakeClient:
    def __init__(self, connected):
        self._connected = connected

    @property
    def is_connected(self):
        return self._connected


class FakeMethodClient:
    """У некоторых версий pymax is_connected — метод, а не свойство."""

    def __init__(self, connected):
        self._connected = connected

    def is_connected(self):
        return self._connected


async def test_heartbeat_updates_marker_when_connected(db):
    await db_messages.set_last_seen_at(100, db_path=db)
    assert await rm._heartbeat_once(FakeClient(True), db_path=db) is True
    assert await db_messages.get_last_seen_at(db_path=db) > 100


async def test_heartbeat_does_not_move_marker_while_disconnected(db):
    """Живой случай 19 октября: MAX отозвал вход, бот ждал SMS-код, а метка
    «на связи» продолжала обновляться, и добор после входа ничего не находил."""
    await db_messages.set_last_seen_at(100, db_path=db)
    assert await rm._heartbeat_once(FakeClient(False), db_path=db) is False
    assert await db_messages.get_last_seen_at(db_path=db) == 100


async def test_heartbeat_works_when_is_connected_is_a_method(db):
    await db_messages.set_last_seen_at(100, db_path=db)
    assert await rm._heartbeat_once(FakeMethodClient(True), db_path=db) is True
    assert await rm._heartbeat_once(FakeMethodClient(False), db_path=db) is False


def test_main_account_backfill_covers_main_and_flat_groups(monkeypatch):
    monkeypatch.setattr(rm, "TG_GROUP_ID", -1001)
    monkeypatch.setattr(rm, "TG_GROUP_FLAT", -1002)
    assert rm._backfill_tg_groups(-1001) == (-1001, -1002)


def test_tenant_backfill_covers_only_its_own_group(monkeypatch):
    monkeypatch.setattr(rm, "TG_GROUP_ID", -1001)
    monkeypatch.setattr(rm, "TG_GROUP_FLAT", -1002)
    assert rm._backfill_tg_groups(-1009) == (-1009,)


class FakeMessage:
    def __init__(self, chat_id, msg_id, when):
        self.chat_id = chat_id
        self.id = msg_id
        self.time = when


class FakeChat:
    def __init__(self, messages):
        self._messages = messages

    async def history(self, backward=30):
        return self._messages


class BackfillClient:
    class me:
        class contact:
            id = 1

    def __init__(self, history_by_chat):
        self._history = history_by_chat

    async def get_chat(self, chat_id):
        return FakeChat(self._history.get(chat_id, []))


async def test_backfill_for_main_account_includes_flat_group_chats(db, monkeypatch):
    monkeypatch.setattr(rm, "TG_GROUP_ID", -1001)
    monkeypatch.setattr(rm, "TG_GROUP_FLAT", -1002)
    async with aiosqlite.connect(db) as conn:
        await conn.execute("INSERT INTO dialogs (max_chat_id, telegram_chat_id, telegram_thread_id) VALUES (?, -1001, 5)", (MAIN_CHAT,))
        await conn.execute("INSERT INTO dialogs (max_chat_id, telegram_chat_id, telegram_thread_id) VALUES (?, -1002, NULL)", (FLAT_CHAT,))
        await conn.execute("INSERT INTO dialogs (max_chat_id, telegram_chat_id, telegram_thread_id) VALUES (?, -1009, 7)", (OTHER_GROUP_CHAT,))
        await conn.commit()
    cutoff = int(time.time()) - 3600
    await db_messages.set_last_seen_at(cutoff, db_path=db)

    fresh = int(time.time()) - 60
    client = BackfillClient({
        MAIN_CHAT: [FakeMessage(MAIN_CHAT, 1, fresh)],
        FLAT_CHAT: [FakeMessage(FLAT_CHAT, 2, fresh), FakeMessage(FLAT_CHAT, 3, cutoff - 500)],  # второе слишком старое
        OTHER_GROUP_CHAT: [FakeMessage(OTHER_GROUP_CHAT, 4, fresh)],
    })
    handled = []

    async def fake_handle(message, c, *, tg_group_id, db_path):
        handled.append((message.chat_id, message.id))

    monkeypatch.setattr(rm, "_handle_max_message", fake_handle)

    await rm._backfill_missed_messages(client, tg_group_id=-1001, db_path=db)

    assert sorted(handled) == [(FLAT_CHAT, 2), (MAIN_CHAT, 1)]  # чужая группа не добирается, старое не трогаем


async def test_backfill_for_tenant_does_not_touch_flat_group(db, monkeypatch):
    monkeypatch.setattr(rm, "TG_GROUP_ID", -1001)
    monkeypatch.setattr(rm, "TG_GROUP_FLAT", -1002)
    async with aiosqlite.connect(db) as conn:
        await conn.execute("INSERT INTO dialogs (max_chat_id, telegram_chat_id, telegram_thread_id) VALUES (?, -1009, 7)", (OTHER_GROUP_CHAT,))
        await conn.execute("INSERT INTO dialogs (max_chat_id, telegram_chat_id, telegram_thread_id) VALUES (?, -1002, NULL)", (FLAT_CHAT,))
        await conn.commit()
    await db_messages.set_last_seen_at(int(time.time()) - 3600, db_path=db)
    fresh = int(time.time()) - 60
    client = BackfillClient({
        OTHER_GROUP_CHAT: [FakeMessage(OTHER_GROUP_CHAT, 4, fresh)],
        FLAT_CHAT: [FakeMessage(FLAT_CHAT, 2, fresh)],
    })
    handled = []

    async def fake_handle(message, c, *, tg_group_id, db_path):
        handled.append((message.chat_id, message.id))

    monkeypatch.setattr(rm, "_handle_max_message", fake_handle)
    await rm._backfill_missed_messages(client, tg_group_id=-1009, db_path=db)

    assert handled == [(OTHER_GROUP_CHAT, 4)]
