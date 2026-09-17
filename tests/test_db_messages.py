import aiosqlite
import db_messages


async def test_topic_collision_detection_and_rename(tmp_path):
    db_path = str(tmp_path / "test.db")
    await db_messages.init_db(db_path=db_path)

    await db_messages.save_thread(111, -100999, 501, topic_name="Иван Петров", db_path=db_path)

    collision = await db_messages.find_topic_name_collision(
        -100999, "Иван Петров", exclude_max_chat_id=222, db_path=db_path
    )
    assert collision is True, "должна быть коллизия для второго Ивана Петрова"

    collision_self = await db_messages.find_topic_name_collision(
        -100999, "Иван Петров", exclude_max_chat_id=111, db_path=db_path
    )
    assert collision_self is False, "не должно быть коллизии с самим собой"

    no_collision = await db_messages.find_topic_name_collision(
        -100999, "Совсем другое имя", exclude_max_chat_id=222, db_path=db_path
    )
    assert no_collision is False, "не должно быть коллизии для уникального имени"

    # save_thread без topic_name не должен затирать уже сохранённое имя (COALESCE)
    await db_messages.save_thread(111, -100999, 501, db_path=db_path)
    mapping = await db_messages.get_thread(111, db_path=db_path)
    assert mapping["topic_name"] == "Иван Петров", f"topic_name затёрся: {mapping}"

    # переименование через save_thread с новым topic_name должно обновлять поле
    await db_messages.save_thread(111, -100999, 501, topic_name="Иван (сосед)", db_path=db_path)
    mapping2 = await db_messages.get_thread(111, db_path=db_path)
    assert mapping2["topic_name"] == "Иван (сосед)", f"topic_name не обновился: {mapping2}"


async def test_get_dialog_by_thread_reverse_lookup(tmp_path):
    """Обратный поиск: по (tg_chat_id, thread_id) узнать max_chat_id/max_sender_id,
    включая случай flat-диалога без темы (thread_id=None)."""
    db_path = str(tmp_path / "test.db")
    await db_messages.init_db(db_path=db_path)

    await db_messages.save_thread(111, -100999, 42, topic_name="Иван", db_path=db_path)
    await db_messages.save_thread(222, -100888, None, topic_name=None, db_path=db_path)

    got = await db_messages.get_dialog_by_thread(-100999, 42, db_path=db_path)
    assert got is not None and got["max_chat_id"] == 111, f"ожидали 111, получили {got}"

    got_none = await db_messages.get_dialog_by_thread(-100888, None, db_path=db_path)
    assert got_none is not None and got_none["max_chat_id"] == 222, f"ожидали 222 (NULL thread), получили {got_none}"

    missing = await db_messages.get_dialog_by_thread(-100999, 999, db_path=db_path)
    assert missing is None, f"ожидали None для несуществующей темы, получили {missing}"


async def test_max_sender_id_backfill_migration(tmp_path):
    """init_db должен подтянуть max_sender_id в dialogs из уже накопленной
    таблицы messages для схемы, созданной до появления этой колонки
    (личные диалоги — да, групповые чаты — не трогать, там разные отправители)."""
    db_path = str(tmp_path / "test.db")

    # 1) Симулируем "старую" схему без max_sender_id в dialogs, но с данными в messages
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE dialogs (
                max_chat_id INTEGER PRIMARY KEY,
                telegram_chat_id INTEGER,
                telegram_thread_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_chat_id INTEGER NOT NULL,
                max_message_id INTEGER NOT NULL,
                tg_chat_id INTEGER NOT NULL,
                tg_thread_id INTEGER,
                tg_message_id INTEGER NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                body_text TEXT,
                max_sender_id INTEGER
            )
        """)
        # личный диалог: max_chat_id условно "Иван", реальный отправитель — другой id
        await db.execute(
            "INSERT INTO dialogs (max_chat_id, telegram_chat_id, telegram_thread_id) VALUES (?, ?, ?)",
            (300000001, -100999, 42),
        )
        await db.execute(
            "INSERT INTO messages (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id, max_sender_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (300000001, 1, -100999, 42, 501, 555555555),
        )
        # групповой чат — max_sender_id в dialogs трогать не должны (там разные отправители)
        await db.execute(
            "INSERT INTO dialogs (max_chat_id, telegram_chat_id, telegram_thread_id) VALUES (?, ?, ?)",
            (-900000000001, -100888, 7),
        )
        await db.commit()

    # 2) Прогоняем init_db (там и ALTER, и бэкфилл-миграция)
    await db_messages.init_db(db_path=db_path)

    # 3) Личный диалог должен получить настоящий sender_id из messages
    mapping = await db_messages.get_thread(300000001, db_path=db_path)
    assert mapping["max_sender_id"] == 555555555, f"бэкфилл не сработал: {mapping}"

    # 4) Обратный поиск по sender_id должен найти этот же диалог
    by_sender = await db_messages.get_thread_by_sender_id(555555555, db_path=db_path)
    assert by_sender is not None and by_sender["max_chat_id"] == 300000001, f"{by_sender}"

    # 5) Групповой чат не должен был получить мусор в max_sender_id
    group_mapping = await db_messages.get_thread(-900000000001, db_path=db_path)
    assert group_mapping["max_sender_id"] is None, f"группе не нужен sender_id: {group_mapping}"

    # 6) get_dialog_by_thread по теме должен вернуть оба id
    dialog = await db_messages.get_dialog_by_thread(-100999, 42, db_path=db_path)
    assert dialog == {"max_chat_id": 300000001, "max_sender_id": 555555555}, f"{dialog}"

    # 7) save_thread с новым max_sender_id для НОВОГО диалога
    await db_messages.save_thread(777, -100999, 88, topic_name="Тест", max_sender_id=999, db_path=db_path)
    new_mapping = await db_messages.get_thread(777, db_path=db_path)
    assert new_mapping["max_sender_id"] == 999, f"{new_mapping}"

    # 8) save_thread без max_sender_id не должен затирать уже сохранённый (COALESCE)
    await db_messages.save_thread(777, -100999, 88, db_path=db_path)
    kept = await db_messages.get_thread(777, db_path=db_path)
    assert kept["max_sender_id"] == 999, f"max_sender_id затёрся: {kept}"
