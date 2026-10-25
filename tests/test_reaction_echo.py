import pytest
from aiogram.types import ReactionTypeCustomEmoji, ReactionTypeEmoji

import db_messages
import relay_max_to_tg as rm
import relay_tg_to_max as r
import state

MAX_CHAT = -500
MAX_MSG = 123
TG_CHAT = -1001110005
TG_MSG = 77
HEART = "❤️"  # с variation selector, как хранит MAX


class FakeUser:
    id = 424242


class FakeChat:
    id = TG_CHAT


class FakeReaction:
    def __init__(self, new_reaction):
        self.chat = FakeChat()
        self.user = FakeUser()
        self.actor_chat = None
        self.message_id = TG_MSG
        self.new_reaction = new_reaction


class FakeMaxClient:
    """Принимает только реакции из набора MAX: «❤️» с селектором, «😢», «👍»."""

    ACCEPTED = {HEART, "😢", "👍"}

    def __init__(self):
        self.added = []
        self.removed = 0
        self.fail_all = False

    async def add_reaction(self, chat_id, message_id, reaction):
        if self.fail_all:
            raise RuntimeError("message.not.found")
        if reaction not in self.ACCEPTED:
            raise RuntimeError("error.message.like.unknown.like")
        self.added.append(reaction)

    async def remove_reaction(self, chat_id, message_id):
        if self.fail_all:
            raise RuntimeError("message.not.found")
        self.removed += 1


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    await db_messages.init_db(db_path=db_path)
    client = FakeMaxClient()
    monkeypatch.setattr(r, "_resolve_route", lambda chat_id: (client, db_path))

    async def mapping(*args, **kwargs):
        return (MAX_CHAT, MAX_MSG)

    monkeypatch.setattr(r, "get_max_message_id", mapping)
    return client, db_path


async def _known(db_path):
    return await db_messages.get_known_reaction(MAX_CHAT, MAX_MSG, db_path=db_path)


async def test_reaction_passed_to_max_is_remembered_as_known(env):
    client, db_path = env
    await r.telegram_reaction_to_max(FakeReaction([ReactionTypeEmoji(emoji="😢")]))
    assert client.added == ["😢"]
    assert await _known(db_path) == "😢"


async def test_heart_without_selector_is_remembered_in_max_form(env):
    client, db_path = env
    await r.telegram_reaction_to_max(FakeReaction([ReactionTypeEmoji(emoji="❤")]))  # Telegram отдаёт без селектора
    assert client.added == [HEART]
    assert await _known(db_path) == HEART


async def test_unsupported_emoji_falls_back_to_heart_and_remembers_it(env):
    client, db_path = env
    await r.telegram_reaction_to_max(FakeReaction([ReactionTypeEmoji(emoji="🐳")]))
    assert client.added == [HEART]
    assert await _known(db_path) == HEART


async def test_custom_emoji_reaction_is_remembered_as_heart(env):
    client, db_path = env
    await r.telegram_reaction_to_max(FakeReaction([ReactionTypeCustomEmoji(custom_emoji_id="1")]))
    assert await _known(db_path) == HEART


async def test_removing_reaction_resets_known_to_none(env):
    client, db_path = env
    await db_messages.set_known_reaction(MAX_CHAT, MAX_MSG, "😢", db_path=db_path)
    await r.telegram_reaction_to_max(FakeReaction([]))
    assert client.removed == 1
    assert await _known(db_path) is None


async def test_failed_transfer_does_not_pretend_reaction_was_set(env):
    client, db_path = env
    client.fail_all = True
    await r.telegram_reaction_to_max(FakeReaction([ReactionTypeEmoji(emoji="😢")]))
    assert await _known(db_path) is None


# --- сквозной случай со скриншота: своя реакция не должна вернуться эхом ---

class _Counter:
    def __init__(self, reaction, count):
        self.reaction = reaction
        self.count = count


class _Info:
    def __init__(self, counters):
        self.counters = counters


class PollClient:
    def __init__(self, counters):
        self._counters = counters

    async def get_reactions(self, chat_id, message_ids):
        return {str(MAX_MSG): _Info(self._counters)}


class FakeBot:
    def __init__(self):
        self.reactions = []

    async def set_message_reaction(self, chat_id, message_id, reaction):
        self.reactions.append((chat_id, message_id, [x.emoji for x in reaction]))


async def _seed_message(db_path):
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO messages (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id, created_at)
               VALUES (?, ?, ?, 3, ?, strftime('%s','now'))""",
            (MAX_CHAT, MAX_MSG, TG_CHAT, TG_MSG),
        )
        await db.commit()


async def test_own_reaction_is_not_echoed_back_to_telegram_by_the_poll(env, monkeypatch):
    client, db_path = env
    bot = FakeBot()
    monkeypatch.setattr(state, "tg_bot", bot)
    await _seed_message(db_path)

    await r.telegram_reaction_to_max(FakeReaction([ReactionTypeEmoji(emoji="😢")]))  # поставил в Telegram
    await rm._poll_reactions_once(PollClient([_Counter("😢", 1)]), db_path=db_path)  # опрос MAX через 30 с

    assert bot.reactions == [], f"эхо реакции от имени бота: {bot.reactions}"


async def test_reaction_made_in_max_still_reaches_telegram(env, monkeypatch):
    client, db_path = env
    bot = FakeBot()
    monkeypatch.setattr(state, "tg_bot", bot)
    await _seed_message(db_path)

    await rm._poll_reactions_once(PollClient([_Counter("👍", 1)]), db_path=db_path)

    assert bot.reactions == [(TG_CHAT, TG_MSG, ["👍"])]


async def test_changing_reaction_in_max_after_own_one_is_still_mirrored(env, monkeypatch):
    client, db_path = env
    bot = FakeBot()
    monkeypatch.setattr(state, "tg_bot", bot)
    await _seed_message(db_path)

    await r.telegram_reaction_to_max(FakeReaction([ReactionTypeEmoji(emoji="😢")]))
    await rm._poll_reactions_once(PollClient([_Counter("😢", 1), _Counter("👍", 2)]), db_path=db_path)

    assert bot.reactions == [(TG_CHAT, TG_MSG, ["👍"])]  # чужая реакция в MAX набрала больше
