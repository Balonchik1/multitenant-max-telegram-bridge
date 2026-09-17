import time

import db_messages
import relay_max_to_tg


class FakeMsg:
    def __init__(self, id_=None, t=None, chat_id=None):
        self.id = id_
        self.time = t
        self.chat_id = chat_id


class FakeChat:
    def __init__(self, history_msgs):
        self._history_msgs = history_msgs

    async def history(self, backward=30):
        return self._history_msgs


class FakeClientMe:
    class Contact:
        id = 99999999
    contact = Contact()


class FakeClient:
    def __init__(self, chats_by_id):
        self.me = FakeClientMe()
        self._chats_by_id = chats_by_id

    async def get_chat(self, chat_id):
        return self._chats_by_id.get(chat_id)


async def test_last_seen_at_roundtrip_and_upsert(tmp_path):
    db_path = str(tmp_path / "test.db")
    await db_messages.init_db(db_path=db_path)

    # без сохранённой метки get_last_seen_at должен вернуть None
    assert await db_messages.get_last_seen_at(db_path=db_path) is None

    now = int(time.time())
    await db_messages.set_last_seen_at(now, db_path=db_path)
    got = await db_messages.get_last_seen_at(db_path=db_path)
    assert got == now, f"{got} != {now}"

    # повторный set должен обновлять (upsert), не падать на дубликате PK
    later = now + 100
    await db_messages.set_last_seen_at(later, db_path=db_path)
    got2 = await db_messages.get_last_seen_at(db_path=db_path)
    assert got2 == later, f"{got2} != {later}"


def test_msg_time_seconds_normalizes_ms_and_seconds():
    now = int(time.time())

    assert relay_max_to_tg._msg_time_seconds(FakeMsg(t=now)) == now, "секунды не должны делиться"
    ms_value = now * 1000
    assert relay_max_to_tg._msg_time_seconds(FakeMsg(t=ms_value)) == now, "миллисекунды должны делиться на 1000"
    assert relay_max_to_tg._msg_time_seconds(FakeMsg(t=0)) == 0
    assert relay_max_to_tg._msg_time_seconds(FakeMsg(t=None)) == 0


def test_time_cutoff_filter_drops_old_keeps_fresh():
    """Та же логика фильтра, что в _backfill_missed_messages: старое
    (недели назад) отсекается, новое (после cutoff) остаётся — это и есть
    защита от рассылки недель старой переписки дубликатами (см. докстринг
    _backfill_missed_messages)."""
    now = int(time.time())
    cutoff = now
    week_ago_msg = FakeMsg(t=(now - 7 * 24 * 3600) * 1000)  # в мс, как реальные
    fresh_msg = FakeMsg(t=(now + 10) * 1000)
    history = [week_ago_msg, fresh_msg]

    recent = [m for m in history if relay_max_to_tg._msg_time_seconds(m) >= cutoff]
    assert recent == [fresh_msg], f"фильтр должен оставить только свежее: {recent}"


async def test_backfill_missed_messages_respects_time_cutoff(tmp_path, monkeypatch):
    """Интеграционный тест _backfill_missed_messages целиком: первый вызов
    без сохранённой метки ничего не досылает (не знаем границы, не гадаем),
    второй вызов после отката метки досылает только то, что новее cutoff."""
    db_path = str(tmp_path / "test.db")
    tg_group_id = -100999
    await db_messages.init_db(db_path=db_path)

    now = int(time.time())
    max_chat_id = 555111222
    await db_messages.save_thread(max_chat_id, tg_group_id, 10, topic_name="Тест", db_path=db_path)

    old_msg = FakeMsg(id_=1001, t=(now - 20 * 24 * 3600) * 1000)  # 20 дней назад, в мс
    new_msg = FakeMsg(id_=1002, t=(now - 5) * 1000)  # 5 секунд назад
    client = FakeClient({max_chat_id: FakeChat([old_msg, new_msg])})

    handled = []

    async def fake_handle(msg, c, *, tg_group_id, db_path):
        handled.append(msg.id)

    monkeypatch.setattr(relay_max_to_tg, "_handle_max_message", fake_handle)

    # Первый вызов: нет сохранённой метки — не должен ничего обработать,
    # только запомнить текущее время
    await relay_max_to_tg._backfill_missed_messages(client, tg_group_id=tg_group_id, db_path=db_path)
    assert handled == [], f"первый вызов не должен ничего досылать: {handled}"
    cutoff_after_first = await db_messages.get_last_seen_at(db_path=db_path)
    assert cutoff_after_first is not None

    # Откатываем метку на 10 секунд назад, имитируя простой
    await db_messages.set_last_seen_at(now - 10, db_path=db_path)

    # Второй вызов: должен подтянуть только new_msg (5с назад > cutoff),
    # но НЕ old_msg (20 дней назад)
    await relay_max_to_tg._backfill_missed_messages(client, tg_group_id=tg_group_id, db_path=db_path)
    assert handled == [1002], f"должен был досослать только новое сообщение: {handled}"
