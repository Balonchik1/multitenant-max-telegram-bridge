import aiosqlite
import pytest

import relay_tg_to_max as r
import sender_policy as sp
import state
import tenants

ADMIN = sp.ADMIN_ID
ADMIN_GROUP = -1001110001
TENANT = 777001
TENANT_GROUP = -1001110002
STRANGER = 888001


class FakeUser:
    def __init__(self, uid, name=None):
        self.id = uid
        if name is not None:
            self.full_name = name


class FakeChat:
    def __init__(self, chat_id, type="supergroup"):
        self.id = chat_id
        self.type = type
        self.is_forum = True


class FakeMessage:
    def __init__(self, chat_id, uid, text="привет", sender_chat=None, thread=5, name=None, **extra):
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser(uid, name) if uid is not None else None
        self.sender_chat = sender_chat
        self.text = text
        self.caption = None
        self.media_group_id = None
        self.message_thread_id = thread
        self.message_id = 10
        self.replies = []
        for attr in ("photo", "voice", "audio", "video", "video_note", "sticker", "document",
                     "forward_from", "forward_origin", "reply_to_message"):
            setattr(self, attr, None)
        for key, value in extra.items():
            setattr(self, key, value)

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class FakeReaction:
    def __init__(self, chat_id, uid, actor_chat=None):
        self.chat = FakeChat(chat_id)
        self.user = FakeUser(uid) if uid is not None else None
        self.actor_chat = actor_chat
        self.message_id = 10
        self.new_reaction = []


class FakeQuery:
    def __init__(self, uid, data):
        self.from_user = FakeUser(uid)
        self.data = data
        self.answers = []
        self.edits = []
        outer = self

        class _Msg:
            async def edit_text(self, text, **kwargs):
                outer.edits.append((text, kwargs.get("reply_markup")))

        self.message = _Msg()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    """Личные уведомления владельцу: (chat_id, text, reply_markup)."""

    def __init__(self):
        self.sent = []
        self.calls = []
        self.fail = False

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})


@pytest.fixture
async def env(tmp_path, monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(state, "tg_bot", bot)
    monkeypatch.setattr(state, "DB_PATH", str(tmp_path / "bridge.db"))
    monkeypatch.setattr(state, "allowed_groups_runtime", {ADMIN_GROUP})
    monkeypatch.setitem(state.tenant_group_map, TENANT_GROUP, TENANT)
    await tenants.init_tenants_table()
    await sp.init_sender_policy_table()
    sp._last_notice.clear()
    sp._last_seen_write.clear()
    sp._last_attempt.clear()
    sp._notify_times.clear()
    yield bot
    sp._allow_all.clear()
    sp._use_prefix.clear()
    sp._people.clear()
    sp._last_notice.clear()
    sp._last_seen_write.clear()
    sp._last_attempt.clear()
    sp._notify_times.clear()
    state.tenant_group_map.pop(TENANT_GROUP, None)


async def _add_tenant(uid, status="active"):
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute("INSERT INTO tenant_accounts (owner_id, status) VALUES (?, ?)", (uid, status))
        await db.commit()


def test_only_owner_is_allowed_by_default(env):
    assert sp.message_allowed(FakeMessage(ADMIN_GROUP, ADMIN))
    assert not sp.message_allowed(FakeMessage(ADMIN_GROUP, STRANGER))
    assert sp.message_allowed(FakeMessage(TENANT_GROUP, TENANT))
    assert not sp.message_allowed(FakeMessage(TENANT_GROUP, STRANGER))
    # владелец одного моста не может писать в группу другого
    assert not sp.message_allowed(FakeMessage(TENANT_GROUP, ADMIN))


def test_anonymous_admin_post_counts_as_owner(env):
    group_as_sender = FakeChat(TENANT_GROUP)
    assert sp.message_allowed(FakeMessage(TENANT_GROUP, 1087968824, sender_chat=group_as_sender))
    other_channel = FakeChat(-1009999)
    assert not sp.message_allowed(FakeMessage(TENANT_GROUP, 1087968824, sender_chat=other_channel))


def test_message_without_author_is_not_allowed(env):
    assert not sp.message_allowed(FakeMessage(ADMIN_GROUP, None))


def test_unknown_chat_is_not_restricted(env):
    assert sp.message_allowed(FakeMessage(-1002223334, STRANGER))


def test_reactions_follow_same_rule(env):
    assert sp.reaction_allowed(FakeReaction(ADMIN_GROUP, ADMIN))
    assert not sp.reaction_allowed(FakeReaction(ADMIN_GROUP, STRANGER))
    assert not sp.reaction_allowed(FakeReaction(ADMIN_GROUP, None))  # анонимная реакция не владельца
    assert sp.reaction_allowed(FakeReaction(ADMIN_GROUP, None, actor_chat=FakeChat(ADMIN_GROUP)))


async def test_allow_all_opens_only_that_owners_group(env):
    await sp.set_allow_all(TENANT, True)
    assert sp.message_allowed(FakeMessage(TENANT_GROUP, STRANGER))
    assert sp.reaction_allowed(FakeReaction(TENANT_GROUP, STRANGER))
    assert not sp.message_allowed(FakeMessage(ADMIN_GROUP, STRANGER))


async def test_setting_survives_restart(env):
    await sp.set_allow_all(ADMIN, True)
    sp._allow_all.clear()  # как после перезапуска: кэш пуст, БД осталась
    assert not sp.allow_all(ADMIN)
    await sp.init_sender_policy_table()
    assert sp.allow_all(ADMIN)


async def test_reset_returns_owner_only(env):
    await sp.set_allow_all(TENANT, True)
    await sp.reset(TENANT)
    assert not sp.allow_all(TENANT)
    sp._allow_all.clear()
    await sp.init_sender_policy_table()
    assert not sp.allow_all(TENANT)


class ExplodingClient:
    def __getattr__(self, name):
        raise AssertionError(f"чужое сообщение дошло до MAX: client.{name}")


async def test_stranger_message_is_not_forwarded_and_gets_one_notice(env, monkeypatch):
    monkeypatch.setattr(r, "_resolve_route", lambda chat_id: (ExplodingClient(), "unused.db"))

    first = FakeMessage(ADMIN_GROUP, STRANGER)
    second = FakeMessage(ADMIN_GROUP, STRANGER, text="ещё одно")
    await r.telegram_to_max(first)
    await r.telegram_to_max(second)

    assert len(first.replies) == 1 and "владельца" in first.replies[0]
    assert "/senders" not in first.replies[0]  # чужому человеку команда ни к чему
    assert second.replies == []  # не заваливаем тему повторами


async def test_service_event_from_stranger_gets_no_notice(env, monkeypatch):
    monkeypatch.setattr(r, "_resolve_route", lambda chat_id: (ExplodingClient(), "unused.db"))
    joined = FakeMessage(ADMIN_GROUP, STRANGER, text=None)
    await r.telegram_to_max(joined)
    assert joined.replies == []


async def test_stranger_edit_and_reaction_are_ignored(env, monkeypatch):
    monkeypatch.setattr(r, "_resolve_route", lambda chat_id: (ExplodingClient(), "unused.db"))

    async def no_lookup(*args, **kwargs):
        raise AssertionError("маппинг не должен запрашиваться для чужих действий")

    monkeypatch.setattr(r, "get_max_message_id", no_lookup)

    await r.telegram_edited_to_max(FakeMessage(ADMIN_GROUP, STRANGER, text="правка"))
    await r.telegram_reaction_to_max(FakeReaction(ADMIN_GROUP, STRANGER))


async def test_owner_edit_still_reaches_lookup(env, monkeypatch):
    monkeypatch.setattr(r, "_resolve_route", lambda chat_id: (ExplodingClient(), "unused.db"))
    looked_up = []

    async def lookup(*args, **kwargs):
        looked_up.append(1)
        return None

    monkeypatch.setattr(r, "get_max_message_id", lookup)
    await r.telegram_edited_to_max(FakeMessage(ADMIN_GROUP, ADMIN, text="правка"))
    assert looked_up == [1]


@pytest.mark.parametrize("text", ["/senders", "/senders@some_bot", " /senders  "])
def test_senders_command_matches_only_in_private(text):
    msg = FakeMessage(1, 5, text=text)
    msg.chat.type = "private"
    assert sp._is_senders_command(msg)
    msg.chat.type = "supergroup"
    assert not sp._is_senders_command(msg)


def test_other_text_is_not_senders_command():
    msg = FakeMessage(1, 5, text="/sendersx")
    msg.chat.type = "private"
    assert not sp._is_senders_command(msg)


def test_senders_command_in_group_gets_private_hint():
    assert r._is_private_only_command("/senders")


async def test_admin_toggle_roundtrip(env):
    msg = FakeMessage(1, ADMIN, text="/senders")
    msg.chat.type = "private"
    markups = []

    async def reply(text, **kwargs):
        msg.replies.append(text)
        markups.append(kwargs.get("reply_markup"))

    msg.reply = reply
    await sp.cmd_senders(msg)
    assert "только" in msg.replies[0].lower()
    assert markups[0].inline_keyboard[0][0].callback_data == f"senders:all:{ADMIN}"

    q = FakeQuery(ADMIN, f"senders:all:{ADMIN}")
    await sp.cb_senders(q)
    assert sp.allow_all(ADMIN)
    assert q.answers == [("Готово", False)]
    text, markup = q.edits[-1]
    assert "всех участников" in text
    assert markup.inline_keyboard[0][0].callback_data == f"senders:owner:{ADMIN}"

    back = FakeQuery(ADMIN, f"senders:owner:{ADMIN}")
    await sp.cb_senders(back)
    assert not sp.allow_all(ADMIN)


async def test_someone_elses_button_is_rejected(env):
    q = FakeQuery(STRANGER, f"senders:all:{ADMIN}")
    await sp.cb_senders(q)
    assert not sp.allow_all(ADMIN)
    assert q.answers == [("Это не твоя кнопка", True)]


async def test_tenant_can_toggle_after_connecting(env):
    await _add_tenant(TENANT)
    await sp.cb_senders(FakeQuery(TENANT, f"senders:all:{TENANT}"))
    assert sp.allow_all(TENANT)


async def test_user_without_connected_account_cannot_toggle(env):
    await _add_tenant(STRANGER, status="awaiting_phone")
    q = FakeQuery(STRANGER, f"senders:all:{STRANGER}")
    await sp.cb_senders(q)
    assert not sp.allow_all(STRANGER)
    assert q.answers[-1][1] is True

    msg = FakeMessage(1, STRANGER, text="/senders")
    msg.chat.type = "private"
    await sp.cmd_senders(msg)
    assert "подключишь MAX" in msg.replies[0]


async def test_garbage_callback_data_is_rejected(env):
    for data in ("senders:all", "senders:maybe:1", "senders:all:abc"):
        q = FakeQuery(ADMIN, data)
        await sp.cb_senders(q)
        assert q.answers and q.answers[0][0] in ("Ошибка данных", "Плохой id")


# --- префикс с именем автора ---

async def test_stranger_gets_name_prefix_only_in_all_mode(env):
    msg = FakeMessage(TENANT_GROUP, STRANGER, name="Иван Петров")
    assert sp.author_prefix(msg) == ""  # режим «только владелец»: префикс не нужен
    await sp.set_allow_all(TENANT, True)
    assert sp.author_prefix(msg) == "**Иван Петров**\n"
    assert sp.author_prefix(FakeMessage(TENANT_GROUP, TENANT, name="Хозяйка")) == ""


async def test_prefix_can_be_switched_off(env):
    await sp.set_allow_all(TENANT, True)
    await sp.set_use_prefix(TENANT, False)
    assert sp.author_prefix(FakeMessage(TENANT_GROUP, STRANGER, name="Иван")) == ""
    await sp.set_use_prefix(TENANT, True)
    assert sp.author_prefix(FakeMessage(TENANT_GROUP, STRANGER, name="Иван")) == "**Иван**\n"


async def test_prefix_name_is_cleaned_and_has_fallback(env):
    await sp.set_allow_all(TENANT, True)
    assert sp.author_prefix(FakeMessage(TENANT_GROUP, STRANGER, name="*Ив_ан*")) == "**Иван**\n"
    assert sp.author_prefix(FakeMessage(TENANT_GROUP, STRANGER, name=None)) == "**Участник группы**\n"
    long_name = sp.author_prefix(FakeMessage(TENANT_GROUP, STRANGER, name="Я" * 100))
    assert len(long_name) <= sp._NAME_MAX_LEN + len("****\n")


def test_with_prefix_handles_empty_text():
    assert sp.with_prefix("**Иван**\n", "привет") == "**Иван**\nпривет"
    assert sp.with_prefix("**Иван**\n", "") == "**Иван**"
    assert sp.with_prefix("", "привет") == "привет"


async def test_prefix_setting_survives_restart_and_old_table_is_migrated(env):
    # таблица, созданная первой версией, без колонки use_prefix
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute("DROP TABLE sender_policy")
        await db.execute("CREATE TABLE sender_policy (owner_id INTEGER PRIMARY KEY, allow_all INTEGER NOT NULL DEFAULT 0)")
        await db.execute("INSERT INTO sender_policy VALUES (?, 1)", (TENANT,))
        await db.commit()
    await sp.init_sender_policy_table()
    assert sp.allow_all(TENANT) and sp.use_prefix(TENANT)  # у старых записей префикс включён

    await sp.set_use_prefix(TENANT, False)
    sp._use_prefix.clear()
    await sp.init_sender_policy_table()
    assert not sp.use_prefix(TENANT)


async def test_reset_brings_prefix_back_to_default(env):
    await sp.set_allow_all(TENANT, True)
    await sp.set_use_prefix(TENANT, False)
    await sp.reset(TENANT)
    assert sp.use_prefix(TENANT) and not sp.allow_all(TENANT)


async def test_prefix_buttons_in_ui(env):
    text, markup = sp._screen(ADMIN)
    # в режиме «только я» кнопки общих имён нет
    assert [row[0].callback_data for row in markup.inline_keyboard] == [
        f"senders:all:{ADMIN}", f"senders:add:{ADMIN}",
    ]

    await sp.cb_senders(FakeQuery(ADMIN, f"senders:all:{ADMIN}"))
    text, markup = sp._screen(ADMIN)
    assert [row[0].callback_data for row in markup.inline_keyboard] == [
        f"senders:owner:{ADMIN}", f"senders:pfxoff:{ADMIN}", f"senders:add:{ADMIN}",
    ]
    assert "жирной строкой" in text

    q = FakeQuery(ADMIN, f"senders:pfxoff:{ADMIN}")
    await sp.cb_senders(q)
    assert not sp.use_prefix(ADMIN)
    shown_text, shown_markup = q.edits[-1]
    assert "Имена авторов не добавляются" in shown_text
    assert shown_markup.inline_keyboard[1][0].callback_data == f"senders:pfxon:{ADMIN}"

    await sp.cb_senders(FakeQuery(ADMIN, f"senders:pfxon:{ADMIN}"))
    assert sp.use_prefix(ADMIN)


class RecordingClient:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)

        class _Sent:
            id = 999

        return _Sent()

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


@pytest.fixture
def relay_env(monkeypatch, tmp_path):
    import sqlite3

    db_path = str(tmp_path / "t.db")
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE dialogs (max_chat_id INTEGER PRIMARY KEY, telegram_chat_id INTEGER, telegram_thread_id INTEGER, "
        "topic_name TEXT, max_sender_id INTEGER)"
    )
    con.execute("INSERT INTO dialogs VALUES (-500, ?, 5, 't', NULL)", (TENANT_GROUP,))
    con.commit()
    con.close()

    client = RecordingClient()
    monkeypatch.setattr(r, "_resolve_route", lambda chat_id: (client, db_path))

    async def no_mapping(*args, **kwargs):
        return (-500, 123)

    async def save_nothing(**kwargs):
        return None

    monkeypatch.setattr(r, "get_max_message_id", no_mapping)
    monkeypatch.setattr(r, "save_message_mapping", save_nothing)
    return client


async def test_stranger_text_reaches_max_with_name(env, relay_env):
    await sp.set_allow_all(TENANT, True)
    await r.telegram_to_max(FakeMessage(TENANT_GROUP, STRANGER, text="привет", name="Иван"))
    assert relay_env.sent[0]["text"] == "**Иван**\nпривет"


async def test_owner_text_reaches_max_untouched(env, relay_env):
    await sp.set_allow_all(TENANT, True)
    await r.telegram_to_max(FakeMessage(TENANT_GROUP, TENANT, text="привет", name="Хозяйка"))
    assert relay_env.sent[0]["text"] == "привет"


async def test_stranger_text_without_prefix_when_switched_off(env, relay_env):
    await sp.set_allow_all(TENANT, True)
    await sp.set_use_prefix(TENANT, False)
    await r.telegram_to_max(FakeMessage(TENANT_GROUP, STRANGER, text="привет", name="Иван"))
    assert relay_env.sent[0]["text"] == "привет"


async def test_stranger_sticker_keeps_label_after_name(env, relay_env, monkeypatch):
    async def fake_download(file_id, suffix=""):
        return "unused.webp"

    async def fake_remove(path):
        return None

    monkeypatch.setattr(r, "download_tg_file", fake_download)
    monkeypatch.setattr(r, "safe_remove", fake_remove)
    monkeypatch.setattr(r, "Photo", lambda path: ("photo", path))
    await sp.set_allow_all(TENANT, True)

    sticker = type("S", (), {"file_size": 10, "is_animated": False, "is_video": False,
                             "emoji": "😀", "file_id": "x"})()
    await r.telegram_to_max(FakeMessage(TENANT_GROUP, STRANGER, text=None, name="Иван", sticker=sticker))
    assert relay_env.sent[0]["text"] == "**Иван**\nСтикер 😀"


async def test_stranger_edit_keeps_name_prefix(env, relay_env):
    await sp.set_allow_all(TENANT, True)
    await r.telegram_edited_to_max(FakeMessage(TENANT_GROUP, STRANGER, text="правка", name="Иван"))
    assert relay_env.edits[0]["text"] == "**Иван**\nправка"

# --- выбор отдельных людей ---

def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def _stranger_tries_to_write(chat_id=TENANT_GROUP, uid=STRANGER, name="Иван Петров"):
    msg = FakeMessage(chat_id, uid, name=name)
    await sp.notify_rejected(msg)
    return msg


async def test_rejected_author_is_remembered_once_per_cooldown(env):
    await _stranger_tries_to_write()
    await _stranger_tries_to_write()
    assert await sp.list_candidates(TENANT) == [(STRANGER, "Иван Петров")]


async def test_service_events_bots_and_channels_are_not_remembered(env):
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, text=None, name="Вошёл"))  # служебное
    bot_msg = FakeMessage(TENANT_GROUP, 999, name="Бот")
    bot_msg.from_user.is_bot = True
    await sp.notify_rejected(bot_msg)
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, 1087968824, name="Channel", sender_chat=FakeChat(-1009)))
    assert await sp.list_candidates(TENANT) == []


async def test_candidate_list_is_capped_to_newest(env):
    for i in range(sp._MAX_CANDIDATES + 5):
        sp._last_seen_write.clear()
        await sp.remember_candidate(TENANT, 1000 + i, f"Человек {i}")
    found = await sp.list_candidates(TENANT)
    assert len(found) == sp._MAX_CANDIDATES


async def test_candidates_are_per_owner(env):
    await _stranger_tries_to_write(TENANT_GROUP)
    await _stranger_tries_to_write(ADMIN_GROUP)
    assert [uid for uid, _ in await sp.list_candidates(TENANT)] == [STRANGER]
    assert [uid for uid, _ in await sp.list_candidates(ADMIN)] == [STRANGER]
    await sp.add_person(ADMIN, STRANGER, "Иван", True)
    assert await sp.list_candidates(ADMIN) == []  # разрешённый из кандидатов уходит
    assert [uid for uid, _ in await sp.list_candidates(TENANT)] == [STRANGER]


async def test_add_flow_with_name(env):
    await _add_tenant(TENANT)
    await _stranger_tries_to_write()

    q = FakeQuery(TENANT, f"senders:add:{TENANT}")
    await sp.cb_senders(q)
    text, markup = q.edits[-1]
    assert (f"👤 Иван Петров", f"senders:pick:{TENANT}:{STRANGER}") in _buttons(markup)

    q = FakeQuery(TENANT, f"senders:pick:{TENANT}:{STRANGER}")
    await sp.cb_senders(q)
    text, markup = q.edits[-1]
    assert "<b>Иван Петров</b> жирной строкой" in text  # пример для MAX
    assert "просто «привет»" in text
    data = [d for _, d in _buttons(markup)]
    assert f"senders:allow1:{TENANT}:{STRANGER}" in data and f"senders:allow0:{TENANT}:{STRANGER}" in data

    q = FakeQuery(TENANT, f"senders:allow1:{TENANT}:{STRANGER}")
    await sp.cb_senders(q)
    assert sp.people(TENANT) == {STRANGER: ("Иван Петров", True)}
    assert "Иван Петров: с именем" in q.edits[-1][0]


async def test_allowed_person_passes_in_owner_only_mode_and_others_do_not(env):
    await sp.add_person(TENANT, STRANGER, "Иван", True)
    assert sp.message_allowed(FakeMessage(TENANT_GROUP, STRANGER))
    assert sp.reaction_allowed(FakeReaction(TENANT_GROUP, STRANGER))
    assert not sp.message_allowed(FakeMessage(TENANT_GROUP, STRANGER + 1))
    # чужой мост этим не затронут
    assert not sp.message_allowed(FakeMessage(ADMIN_GROUP, STRANGER))


async def test_person_prefix_follows_own_setting_even_in_all_mode(env):
    await sp.set_allow_all(TENANT, True)
    await sp.set_use_prefix(TENANT, True)
    await sp.add_person(TENANT, STRANGER, "Жена", False)  # без имени
    assert sp.author_prefix(FakeMessage(TENANT_GROUP, STRANGER, name="Жена")) == ""
    assert sp.author_prefix(FakeMessage(TENANT_GROUP, STRANGER + 1, name="Гость")) == "**Гость**\n"


async def test_person_with_name_gets_prefix_in_owner_only_mode(env):
    await sp.add_person(TENANT, STRANGER, "Иван", True)
    assert sp.author_prefix(FakeMessage(TENANT_GROUP, STRANGER, name="Иван")) == "**Иван**\n"


async def test_add_without_name_flow_and_toggle_and_remove(env):
    await _add_tenant(TENANT)
    await _stranger_tries_to_write()
    await sp.cb_senders(FakeQuery(TENANT, f"senders:allow0:{TENANT}:{STRANGER}"))
    assert sp.people(TENANT)[STRANGER][1] is False

    q = FakeQuery(TENANT, f"senders:person:{TENANT}:{STRANGER}")
    await sp.cb_senders(q)
    assert f"senders:up1:{TENANT}:{STRANGER}" in [d for _, d in _buttons(q.edits[-1][1])]

    await sp.cb_senders(FakeQuery(TENANT, f"senders:up1:{TENANT}:{STRANGER}"))
    assert sp.people(TENANT)[STRANGER][1] is True

    q = FakeQuery(TENANT, f"senders:rm:{TENANT}:{STRANGER}")
    await sp.cb_senders(q)
    assert sp.people(TENANT) == {}
    assert not sp.message_allowed(FakeMessage(TENANT_GROUP, STRANGER))


async def test_picking_someone_not_in_candidates_is_refused(env):
    await _add_tenant(TENANT)
    q = FakeQuery(TENANT, f"senders:allow1:{TENANT}:424242")
    await sp.cb_senders(q)
    assert sp.people(TENANT) == {}
    assert q.answers[0][1] is True


async def test_people_survive_restart_and_reset_clears_everything(env):
    await sp.add_person(TENANT, STRANGER, "Иван", False)
    await _stranger_tries_to_write(uid=STRANGER + 7, name="Другой")
    sp._people.clear()
    await sp.init_sender_policy_table()
    assert sp.people(TENANT) == {STRANGER: ("Иван", False)}

    await sp.reset(TENANT)
    assert sp.people(TENANT) == {}
    assert await sp.list_candidates(TENANT) == []
    await sp.init_sender_policy_table()
    assert sp.people(TENANT) == {}


async def test_names_are_html_escaped_on_screens(env):
    await sp.add_person(TENANT, STRANGER, "<b>Хак</b> & Ко", True)
    text, markup = sp._screen(TENANT)
    assert "<b>Хак</b>" not in text and "&lt;b&gt;Хак&lt;/b&gt; &amp; Ко" in text
    person_text, _ = sp._person_screen(TENANT, STRANGER)
    assert "<b>Хак</b>" not in person_text


async def test_person_buttons_need_matching_shape_and_owner(env):
    await _add_tenant(TENANT)
    q = FakeQuery(TENANT, f"senders:rm:{TENANT}")  # нет id человека
    await sp.cb_senders(q)
    assert q.answers == [("Ошибка данных", False)]

    q = FakeQuery(TENANT, f"senders:all:{TENANT}:5")  # лишний хвост
    await sp.cb_senders(q)
    assert q.answers == [("Ошибка данных", False)]

    await sp.add_person(TENANT, STRANGER, "Иван", True)
    q = FakeQuery(STRANGER, f"senders:rm:{TENANT}:{STRANGER}")  # чужая кнопка
    await sp.cb_senders(q)
    assert sp.people(TENANT) != {}
    assert q.answers == [("Это не твоя кнопка", True)]


async def test_rejection_notice_mentions_senders_and_remembers_author(env, monkeypatch):
    monkeypatch.setattr(r, "_resolve_route", lambda chat_id: (ExplodingClient(), "unused.db"))
    msg = FakeMessage(TENANT_GROUP, STRANGER, name="Иван Петров")
    await r.telegram_to_max(msg)
    assert "/senders" not in msg.replies[0]
    assert await sp.list_candidates(TENANT) == [(STRANGER, "Иван Петров")]

# --- уведомление владельцу о новом человеке ---

def _cb_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_owner_gets_private_notice_once_for_a_new_person(env):
    msg = FakeMessage(TENANT_GROUP, STRANGER, text="секретный текст", name="Иван Петров")
    msg.chat.title = "Семейная"
    await sp.notify_rejected(msg)

    assert len(env.sent) == 1
    chat_id, text, markup = env.sent[0]
    assert chat_id == TENANT
    assert "Иван Петров" in text and "«Семейная»" in text
    assert "секретный текст" not in text  # содержимое сообщения владельцу не пересылаем
    assert _cb_data(markup) == [
        f"senders:allow1:{TENANT}:{STRANGER}",
        f"senders:allow0:{TENANT}:{STRANGER}",
        f"senders:dismiss:{TENANT}:{STRANGER}",
    ]

    # ни повтор в той же теме, ни «после перезапуска» (кэш записей пуст) не дублируют уведомление
    sp._last_notice.clear()
    sp._last_seen_write.clear()
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="Иван Петров"))
    assert len(env.sent) == 1


async def test_admin_group_notice_goes_to_admin(env):
    await sp.notify_rejected(FakeMessage(ADMIN_GROUP, STRANGER, name="Гость"))
    assert [chat for chat, _, _ in env.sent] == [ADMIN]


async def test_no_notice_for_service_events_bots_channels_and_allowed_people(env):
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, text=None, name="Вошёл"))
    bot_msg = FakeMessage(TENANT_GROUP, 999, name="Бот")
    bot_msg.from_user.is_bot = True
    await sp.notify_rejected(bot_msg)
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, 1087968824, name="Channel", sender_chat=FakeChat(-1009)))
    assert env.sent == []

    await sp.add_person(TENANT, STRANGER, "Иван", True)
    assert sp.message_allowed(FakeMessage(TENANT_GROUP, STRANGER))  # до уведомления дело не доходит


async def test_notices_are_rate_limited_but_people_are_still_listed(env):
    for i in range(sp._NOTIFY_PER_HOUR + 3):
        await sp.notify_rejected(FakeMessage(TENANT_GROUP, 5000 + i, name=f"Человек {i}", thread=100 + i))
    assert len(env.sent) == sp._NOTIFY_PER_HOUR
    assert len(await sp.list_candidates(TENANT)) == sp._NOTIFY_PER_HOUR + 3


async def test_failed_notice_does_not_break_rejection(env):
    env.fail = True
    msg = FakeMessage(TENANT_GROUP, STRANGER, name="Иван")
    await sp.notify_rejected(msg)
    assert len(msg.replies) == 1  # человеку всё равно объяснили, что не ушло
    assert await sp.list_candidates(TENANT) == [(STRANGER, "Иван")]


async def test_allow_button_from_notice_adds_person(env):
    await _add_tenant(TENANT)
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="Иван Петров"))
    q = FakeQuery(TENANT, f"senders:allow0:{TENANT}:{STRANGER}")
    await sp.cb_senders(q)
    assert sp.people(TENANT) == {STRANGER: ("Иван Петров", False)}
    assert "Разрешены отдельно" in q.edits[-1][0]  # уведомление превратилось в экран настроек


async def test_dismiss_button_leaves_person_unallowed_but_listed(env):
    await _add_tenant(TENANT)
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="Иван Петров"))
    q = FakeQuery(TENANT, f"senders:dismiss:{TENANT}:{STRANGER}")
    await sp.cb_senders(q)
    assert sp.people(TENANT) == {}
    assert not sp.message_allowed(FakeMessage(TENANT_GROUP, STRANGER))
    assert [uid for uid, _ in await sp.list_candidates(TENANT)] == [STRANGER]
    assert q.answers == [("Хорошо", False)]
    assert "Хорошо" in q.edits[-1][0]


async def test_someone_elses_dismiss_is_rejected(env):
    q = FakeQuery(STRANGER, f"senders:dismiss:{TENANT}:{STRANGER}")
    await sp.cb_senders(q)
    assert q.answers == [("Это не твоя кнопка", True)]


async def test_notice_names_are_html_escaped(env):
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="<b>Хак</b> & Ко"))
    text = env.sent[0][1]
    assert "<b>Хак</b>" not in text and "&lt;b&gt;Хак&lt;/b&gt; &amp; Ко" in text

# --- ответ в группе после разрешения ---

def _announcements(bot):
    return [c for c in bot.calls if c["chat_id"] == TENANT_GROUP]


async def test_allowing_replies_in_group_to_the_message_that_did_not_go(env):
    await _add_tenant(TENANT)
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="Иван Петров", thread=7, message_id=41))
    await sp.cb_senders(FakeQuery(TENANT, f"senders:allow1:{TENANT}:{STRANGER}"))

    [call] = _announcements(env)
    assert call["message_thread_id"] == 7
    assert call["reply_parameters"].message_id == 41
    assert call["reply_parameters"].allow_sending_without_reply is True
    assert "Иван Петров теперь может писать в MAX" in call["text"]
    assert "ещё раз" in call["text"]


async def test_reply_targets_the_latest_attempt(env):
    await _add_tenant(TENANT)
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="Иван", message_id=41))
    sp._last_notice.clear()
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="Иван", message_id=45))  # в пределах кулдауна записи
    await sp.cb_senders(FakeQuery(TENANT, f"senders:allow0:{TENANT}:{STRANGER}"))
    assert _announcements(env)[0]["reply_parameters"].message_id == 45


async def test_reply_target_survives_restart_from_database(env):
    await _add_tenant(TENANT)
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="Иван", thread=9, message_id=52))
    sp._last_attempt.clear()  # как после перезапуска: память пуста
    await sp.cb_senders(FakeQuery(TENANT, f"senders:allow1:{TENANT}:{STRANGER}"))
    [call] = _announcements(env)
    assert call["message_thread_id"] == 9 and call["reply_parameters"].message_id == 52


async def test_no_known_attempt_means_no_announcement_but_person_is_still_allowed(env):
    await _add_tenant(TENANT)
    await sp.remember_candidate(TENANT, STRANGER, "Иван")  # без адреса сообщения
    await sp.cb_senders(FakeQuery(TENANT, f"senders:allow1:{TENANT}:{STRANGER}"))
    assert _announcements(env) == []
    assert STRANGER in sp.people(TENANT)


async def test_failed_announcement_does_not_undo_allowing(env):
    await _add_tenant(TENANT)
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="Иван", message_id=41))
    env.fail = True
    q = FakeQuery(TENANT, f"senders:allow1:{TENANT}:{STRANGER}")
    await sp.cb_senders(q)
    assert STRANGER in sp.people(TENANT)
    assert q.answers == [("Готово", False)]


async def test_dismiss_does_not_announce_anything_in_group(env):
    await _add_tenant(TENANT)
    await sp.notify_rejected(FakeMessage(TENANT_GROUP, STRANGER, name="Иван", message_id=41))
    await sp.cb_senders(FakeQuery(TENANT, f"senders:dismiss:{TENANT}:{STRANGER}"))
    assert _announcements(env) == []


async def test_old_seen_table_without_target_columns_is_migrated(env):
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute("DROP TABLE sender_seen")
        await db.execute(
            "CREATE TABLE sender_seen (owner_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
            "name TEXT NOT NULL, seen_at INTEGER NOT NULL, PRIMARY KEY (owner_id, user_id))"
        )
        await db.execute("INSERT INTO sender_seen VALUES (?, ?, 'Старый', 1)", (TENANT, STRANGER))
        await db.commit()
    await sp.init_sender_policy_table()
    assert await sp.get_attempt_target(TENANT, STRANGER) is None  # у старой записи адреса нет
    await sp.remember_candidate(TENANT, STRANGER + 1, "Новый", (TENANT_GROUP, 3, 77))
    sp._last_attempt.clear()
    assert await sp.get_attempt_target(TENANT, STRANGER + 1) == (TENANT_GROUP, 3, 77)