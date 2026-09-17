import os

import aiosqlite
from aiogram.types import ReactionTypeEmoji, ReactionTypeCustomEmoji

import db_messages


async def test_get_recent_messages_for_reaction_poll_filters_by_time(tmp_path):
    db_path = str(tmp_path / "test.db")
    await db_messages.init_db(db_path=db_path)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO messages (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id, created_at)
               VALUES (1, 100, 2, 3, 400, strftime('%s','now'))"""
        )
        await db.execute(
            """INSERT INTO messages (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id, created_at)
               VALUES (1, 101, 2, 3, 401, strftime('%s','now','-10 hours'))"""
        )
        await db.commit()

    rows = await db_messages.get_recent_messages_for_reaction_poll(hours=2, db_path=db_path)
    assert len(rows) == 1 and rows[0][1] == 100, f"ожидал только свежее сообщение, получил {rows}"


async def test_known_reaction_roundtrip_and_reset_to_none(tmp_path):
    db_path = str(tmp_path / "test.db")
    await db_messages.init_db(db_path=db_path)

    assert await db_messages.get_known_reaction(1, 100, db_path=db_path) is None

    await db_messages.set_known_reaction(1, 100, "❤️", db_path=db_path)
    assert await db_messages.get_known_reaction(1, 100, db_path=db_path) == "❤️"

    await db_messages.set_known_reaction(1, 100, None, db_path=db_path)
    assert await db_messages.get_known_reaction(1, 100, db_path=db_path) is None


async def test_cleanup_removes_orphaned_message_reactions(tmp_path):
    """message_reactions не имеет своего created_at — чистится по
    осиротевшим строкам, у которых уже нет соответствующего маппинга в
    messages, а не по времени (см. докстринг cleanup_old_messages)."""
    db_path = str(tmp_path / "test.db")
    await db_messages.init_db(db_path=db_path)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO message_reactions (max_chat_id, max_message_id, reaction) VALUES (999, 999, '👍')"
        )
        await db.commit()

    await db_messages.cleanup_old_messages(days=30, db_path=db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM message_reactions WHERE max_chat_id=999"
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == 0, "осиротевшая строка message_reactions должна была удалиться"


def _classify(new_reaction):
    """Та же логика различения обычной/кастомной эмодзи-реакции, что и в
    telegram_reaction_to_max (relay_tg_to_max.py) — см. докстринг там
    про ReactionTypeCustomEmoji/ReactionTypePaid у Telegram Premium."""
    emoji = None
    is_non_emoji_reaction = False
    for r in new_reaction:
        if isinstance(r, ReactionTypeEmoji):
            emoji = r.emoji
            break
    else:
        is_non_emoji_reaction = bool(new_reaction)
    return emoji, is_non_emoji_reaction


def test_classify_plain_emoji_reaction():
    assert _classify([ReactionTypeEmoji(emoji="👍")]) == ("👍", False)


def test_classify_removed_reaction():
    assert _classify([]) == (None, False)


def test_classify_custom_emoji_reaction_is_not_treated_as_removed():
    """Premium-реакция кастомным эмодзи — единственный элемент, не
    ReactionTypeEmoji. Раньше это ошибочно трактовалось как "реакцию
    сняли"."""
    assert _classify([ReactionTypeCustomEmoji(custom_emoji_id="123")]) == (None, True)


def test_classify_mixed_list_finds_plain_emoji():
    result = _classify([ReactionTypeCustomEmoji(custom_emoji_id="123"), ReactionTypeEmoji(emoji="🔥")])
    assert result == ("🔥", False)
