"""
Схема БД, маппинг сообщений/тредов MAX <-> Telegram, подстановка pings.

Отдельно от db_meta.py: там — CRUD для служебных таблиц (ping_map,
aliases, mention_map), здесь — схема + таблицы messages/dialogs/allowed_groups
и всё, что с ними работает.
"""
import re

import aiosqlite

import state
from config import ALLOWED_GROUPS, TG_GROUP_ID
from formatters import safe_html
from db_meta import list_pings, get_alias


async def init_db(db_path: str | None = None):
    db_path = db_path or state.DB_PATH
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dialogs (
                max_chat_id INTEGER PRIMARY KEY,
                telegram_chat_id INTEGER,
                telegram_thread_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_chat_id INTEGER NOT NULL,
                max_message_id INTEGER NOT NULL,
                tg_chat_id INTEGER NOT NULL,
                tg_thread_id INTEGER,
                tg_message_id INTEGER NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                UNIQUE(max_chat_id, max_message_id),
                UNIQUE(tg_chat_id, tg_message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS allowed_groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                is_flat INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS aliases (
                max_user_id INTEGER PRIMARY KEY,
                last_name TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_users (
                max_user_id INTEGER PRIMARY KEY,
                first_name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ping_map (
                keyword TEXT PRIMARY KEY,
                tg_username TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mention_map (
                max_user_id INTEGER PRIMARY KEY,
                tg_username TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_seen_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS message_reactions (
                max_chat_id INTEGER NOT NULL,
                max_message_id INTEGER NOT NULL,
                reaction TEXT,
                PRIMARY KEY (max_chat_id, max_message_id)
            )
        """)
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN max_sender_id INTEGER")
        except Exception:
            pass
        # На случай, если таблица уже существовала без created_at
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN created_at INTEGER DEFAULT (strftime('%s', 'now'))")
            await db.execute("UPDATE messages SET created_at = strftime('%s', 'now') WHERE created_at IS NULL")
            state.log.info("Добавлена колонка created_at в таблицу messages")
        except Exception:
            pass  # колонка уже есть
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN body_text TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE ping_map ADD COLUMN tg_id INTEGER")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE mention_map ADD COLUMN tg_id INTEGER")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE dialogs ADD COLUMN topic_name TEXT")
        except Exception:
            pass
        try:
            # Реальный MAX user id собеседника для личного диалога — max_chat_id
            # для личных диалогов НЕ совпадает с его id (это отдельный id самого
            # диалога, может случайно численно совпасть с id другого реального
            # пользователя MAX). get_user(max_chat_id) в таком случае возвращал
            # чужой профиль без единой ошибки — переименование по /alias
            # могло увести имя темы на случайного другого человека.
            # Настоящий id собеседника надёжно
            # берётся только из message.sender при создании/пересоздании темы —
            # сохраняем его сюда и используем везде вместо max_chat_id.
            await db.execute("ALTER TABLE dialogs ADD COLUMN max_sender_id INTEGER")
        except Exception:
            pass
        try:
            # Разовый бэкфилл для диалогов, созданных до этой колонки —
            # подтягиваем max_sender_id из уже накопленной таблицы messages
            # (там он для личных диалогов сохранялся и раньше). Идемпотентно:
            # трогает только ещё не заполненные строки.
            await db.execute(
                """
                UPDATE dialogs
                SET max_sender_id = (
                    SELECT m.max_sender_id FROM messages m
                    WHERE m.max_chat_id = dialogs.max_chat_id AND m.max_sender_id IS NOT NULL
                    ORDER BY m.created_at DESC LIMIT 1
                )
                WHERE dialogs.max_chat_id > 0 AND dialogs.max_sender_id IS NULL
                """
            )
        except Exception as e:
            state.log.warning(f"Бэкфилл max_sender_id пропущен: {e}")
        await db.commit()


async def cleanup_old_messages(days: int = 30, db_path: str | None = None):
    """Удаляет маппинги сообщений старше N дней."""
    db_path = db_path or state.DB_PATH
    try:
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "DELETE FROM messages WHERE created_at < strftime('%s', 'now', ?)",
                (f'-{days} days',)
            )
            deleted = cursor.rowcount
            # message_reactions не имеет своего created_at (опрос реакций
            # обновляет её независимо от возраста сообщения) — чистим по
            # осиротевшим строкам, у которых уже нет соответствующего
            # маппинга в messages, а не по времени.
            await db.execute(
                """
                DELETE FROM message_reactions
                WHERE (max_chat_id, max_message_id) NOT IN (
                    SELECT max_chat_id, max_message_id FROM messages
                )
                """
            )
            await db.commit()
            if deleted:
                state.log.info(f"Очистка: удалено {deleted} старых маппингов сообщений (старше {days} дней)")
            return deleted
    except Exception as e:
        state.log.warning(f"Очистка пропущена: {e}")
        return 0


async def get_last_seen_at(db_path: str | None = None) -> int | None:
    """Unix-время (секунды) последнего подтверждённого 'на связи' для этого
    аккаунта — обновляется периодическим heartbeat'ом, пока клиент MAX
    подключён (см. relay_max_to_tg.py). Используется как граница для добора
    пропущенных сообщений: то, что старше этой метки, не трогаем, даже если
    его уже нет в messages — это не пропущенное, а просто старая переписка,
    которую бот никогда не должен был пересылать."""
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        async with db.execute("SELECT last_seen_at FROM sync_state WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_last_seen_at(ts: int, db_path: str | None = None):
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO sync_state (id, last_seen_at) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (ts,),
        )
        await db.commit()


async def load_allowed_groups() -> set[int]:
    groups = set(ALLOWED_GROUPS)  # из config
    async with aiosqlite.connect(state.DB_PATH) as db:
        async with db.execute("SELECT chat_id FROM allowed_groups") as cur:
            rows = await cur.fetchall()
            for (cid,) in rows:
                groups.add(cid)
    return groups


async def apply_pings(text: str, db_path: str | None = None) -> str:
    if not text:
        return text
    rows = await list_pings(db_path=db_path)
    if not rows:
        return text

    rows = sorted(rows, key=lambda r: len(str(r[0])), reverse=True)
    result = text
    for row in rows:
        keyword = row[0]
        uname = (row[1] or "").lstrip("@")
        tg_id = row[2] if len(row) > 2 else None
        if not keyword:
            continue
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)

        def _repl(m, _uname=uname, _tid=tg_id):
            word = safe_html(m.group(0))
            if _tid:
                return f'<a href="tg://user?id={int(_tid)}">{word}</a>'
            if _uname:
                return f"@{_uname}"
            return word

        result = pattern.sub(_repl, result)
    return result


async def remember_user(max_user_id: int, first_name: str | None, db_path: str | None = None):
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO seen_users (max_user_id, first_name) VALUES (?, ?)
            ON CONFLICT(max_user_id) DO UPDATE SET
                first_name = COALESCE(excluded.first_name, seen_users.first_name)
            """,
            (max_user_id, first_name),
        )
        await db.commit()


async def format_sender_name(client, sender_id: int, db_path: str | None = None) -> str:
    """
    алиас-слово  → Имя Фамилия
    алиас "0"    → только Имя
    фамилия MAX  → Имя Фамилия
    иначе        → Имя 987654321
    """
    first = "Unknown"
    max_last = ""
    try:
        user = await client.get_user(sender_id)
        if user and user.names:
            n = user.names[0]
            first = (n.first_name or n.name or first).strip()
            max_last = (n.last_name or "").strip()
        await remember_user(sender_id, first, db_path=db_path)
    except Exception:
        await remember_user(sender_id, None, db_path=db_path)

    alias = await get_alias(sender_id, db_path=db_path)
    if alias is not None:
        if alias == "0":
            return first
        return f"{first} {alias}".strip()

    if max_last:
        return f"{first} {max_last}".strip()

    return f"{first} {sender_id}"


async def get_thread(max_chat_id: int, db_path: str | None = None, default_tg_group_id: int | None = None):
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        async with db.execute(
            """
            SELECT telegram_chat_id, telegram_thread_id, topic_name, max_sender_id
            FROM dialogs
            WHERE max_chat_id = ?
            """,
            (max_chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "telegram_chat_id": row[0] or (default_tg_group_id if default_tg_group_id is not None else TG_GROUP_ID),
                "telegram_thread_id": row[1],
                "topic_name": row[2],
                "max_sender_id": row[3],
            }


async def get_thread_by_sender_id(max_sender_id: int, db_path: str | None = None):
    """Обратный поиск личного диалога по настоящему MAX user id собеседника
    (не по max_chat_id — см. докстринг колонки max_sender_id в init_db)."""
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        async with db.execute(
            """
            SELECT max_chat_id, telegram_chat_id, telegram_thread_id, topic_name
            FROM dialogs
            WHERE max_sender_id = ?
            """,
            (max_sender_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "max_chat_id": row[0],
                "telegram_chat_id": row[1],
                "telegram_thread_id": row[2],
                "topic_name": row[3],
            }


async def get_dialog_by_thread(tg_chat_id: int, thread_id: int | None, db_path: str | None = None):
    """Обратный поиск: по теме в TG узнать, какому MAX-диалогу она
    соответствует — и chat_id, и (для личных диалогов) настоящий id
    собеседника (используется в /alias, чтобы показать пример с реальным
    id, если команду написали прямо в теме конкретного диалога)."""
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        if thread_id is None:
            query = (
                "SELECT max_chat_id, max_sender_id FROM dialogs "
                "WHERE telegram_chat_id = ? AND telegram_thread_id IS NULL"
            )
            params = (tg_chat_id,)
        else:
            query = (
                "SELECT max_chat_id, max_sender_id FROM dialogs "
                "WHERE telegram_chat_id = ? AND telegram_thread_id = ?"
            )
            params = (tg_chat_id, thread_id)
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {"max_chat_id": row[0], "max_sender_id": row[1]}


async def find_topic_name_collision(
    telegram_chat_id: int, name: str, exclude_max_chat_id: int, db_path: str | None = None
) -> bool:
    """Есть ли в этой же TG-группе уже другая тема с таким же названием
    (используется только для случая, когда у контакта ЕСТЬ и имя, и
    фамилия — иначе сработает более простая проверка на отсутствие фамилии)."""
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        async with db.execute(
            """
            SELECT 1 FROM dialogs
            WHERE telegram_chat_id = ? AND max_chat_id != ? AND topic_name IS NOT NULL
              AND LOWER(topic_name) = LOWER(?)
            LIMIT 1
            """,
            (telegram_chat_id, exclude_max_chat_id, name),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def save_message_mapping(
    max_chat_id: int,
    max_message_id: int,
    tg_chat_id: int,
    tg_thread_id: int | None,
    tg_message_id: int,
    body_text: str | None = None,
    max_sender_id: int | None = None,
    db_path: str | None = None,
):
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO messages
            (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id, created_at, body_text, max_sender_id)
            VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'), ?, ?)
            """,
            (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id, body_text, max_sender_id,),
        )
        await db.commit()


async def get_max_message_id(tg_chat_id: int, tg_message_id: int, db_path: str | None = None) -> tuple[int, int] | None:
    """Возвращает (max_chat_id, max_message_id) или None"""
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        async with db.execute(
            """
            SELECT max_chat_id, max_message_id
            FROM messages
            WHERE tg_chat_id = ? AND tg_message_id = ?
            """,
            (tg_chat_id, tg_message_id)
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else None


async def get_tg_message_id(max_chat_id: int, max_message_id: int, db_path: str | None = None) -> tuple[int, int | None, int] | None:
    """Возвращает (tg_chat_id, tg_thread_id, tg_message_id) или None"""
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        async with db.execute(
            """
            SELECT tg_chat_id, tg_thread_id, tg_message_id
            FROM messages
            WHERE max_chat_id = ? AND max_message_id = ?
            """,
            (max_chat_id, max_message_id)
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0], row[1], row[2]) if row else None


async def get_recent_messages_for_reaction_poll(hours: int = 2, db_path: str | None = None):
    """Сообщения младше N часов — область опроса реакций (get_reactions):
    push от MAX на изменение реакции не приходит совсем (get_reactions видит
    реакцию, событие reaction_update — никогда), так что единственный
    рабочий способ узнать про MAX-реакцию — периодически
    спрашивать сервер самим. Спрашивать про все сообщения без ограничения
    по времени было бы неограниченно растущим и бессмысленным (реакции
    почти всегда ставят вскоре после отправки)."""
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        async with db.execute(
            """
            SELECT max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id
            FROM messages
            WHERE created_at >= strftime('%s', 'now', ?)
            """,
            (f'-{hours} hours',),
        ) as cursor:
            return await cursor.fetchall()


async def get_known_reaction(max_chat_id: int, max_message_id: int, db_path: str | None = None) -> str | None:
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        async with db.execute(
            "SELECT reaction FROM message_reactions WHERE max_chat_id = ? AND max_message_id = ?",
            (max_chat_id, max_message_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_known_reaction(max_chat_id: int, max_message_id: int, reaction: str | None, db_path: str | None = None):
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO message_reactions (max_chat_id, max_message_id, reaction)
            VALUES (?, ?, ?)
            ON CONFLICT(max_chat_id, max_message_id) DO UPDATE SET reaction = excluded.reaction
            """,
            (max_chat_id, max_message_id, reaction),
        )
        await db.commit()


async def save_thread(
    max_chat_id: int, telegram_chat_id: int, thread_id: int | None,
    topic_name: str | None = None, max_sender_id: int | None = None, db_path: str | None = None,
):
    async with aiosqlite.connect(db_path or state.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO dialogs (max_chat_id, telegram_chat_id, telegram_thread_id, topic_name, max_sender_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(max_chat_id) DO UPDATE SET
                telegram_chat_id = excluded.telegram_chat_id,
                telegram_thread_id = excluded.telegram_thread_id,
                topic_name = COALESCE(excluded.topic_name, dialogs.topic_name),
                max_sender_id = COALESCE(excluded.max_sender_id, dialogs.max_sender_id)
            """,
            (max_chat_id, telegram_chat_id, thread_id, topic_name, max_sender_id)
        )
        await db.commit()
