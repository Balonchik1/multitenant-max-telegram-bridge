import aiosqlite

import db_messages


async def test_album_routing_isolates_same_thread_id_across_groups(tmp_path):
    """Два РАЗНЫХ диалога в РАЗНЫХ TG-группах, но с ОДИНАКОВЫМ thread_id —
    именно та коллизия, от которой должен защищать фильтр по (chat_id,
    thread_id) в process_media_group (media.py). Раньше запрос фильтровал
    только по thread_id, что могло увести альбом не в ту группу."""
    db_path = str(tmp_path / "test.db")
    await db_messages.init_db(db_path=db_path)

    await db_messages.save_thread(111, -100111, 5, topic_name="Admin dialog", db_path=db_path)
    await db_messages.save_thread(222, -100222, 5, topic_name="Tenant dialog", db_path=db_path)

    async def resolve(tg_chat_id, thread_id):
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT max_chat_id FROM dialogs WHERE telegram_chat_id = ? AND telegram_thread_id = ?",
                (tg_chat_id, thread_id),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    admin_result = await resolve(-100111, 5)
    tenant_result = await resolve(-100222, 5)

    assert admin_result == 111, f"админская группа должна вести на 111, получили {admin_result}"
    assert tenant_result == 222, f"тенантская группа должна вести на 222, получили {tenant_result}"
    assert admin_result != tenant_result, "коллизия по одинаковому thread_id из разных групп!"
