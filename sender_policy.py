"""
Кто из участников Telegram-группы может писать в MAX от имени владельца моста.

Мост шлёт в MAX от лица ОДНОГО аккаунта — владельца. По умолчанию из группы в
MAX уходит только то, что сделал сам владелец: его сообщения, правки и реакции.
Если в группу зашёл ещё кто-то (владелец добавил человека, или группа плоская и
общая), его действия бот игнорирует.

Командой /senders в личке с ботом владелец может:
  - разрешить писать в MAX конкретным людям (бот запоминает тех, кто пытался
    написать, и показывает их кнопками: список участников группы Telegram
    боту не отдаёт);
  - разрешить писать всем участникам группы.

К сообщениям других людей бот добавляет имя автора (жирной строкой над текстом), чтобы в
MAX было видно, кто написал; сообщения самого владельца идут без имени. Для
каждого разрешённого человека имя включается отдельно, для режима «всем» есть
общий переключатель (например, одним аккаунтом пользуется семья, и в общем
чате имена не нужны).

Всё хранится в основной bridge.db (таблицы sender_policy, sender_people,
sender_seen), настройки кэшируются в памяти. Нет записей — значит «только
владелец».
"""
import html
import re
import time

import aiosqlite
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message as TgMessage,
    ReplyParameters,
)

from config import ADMIN_ID
import state

_NOTICE_COOLDOWN = 600  # секунд между напоминаниями в одной теме
_SEEN_WRITE_COOLDOWN = 300  # как часто обновляем запись «пытался написать» для одного человека
_MAX_CANDIDATES = 20
_MAX_PEOPLE = 30
_PAYLOAD_ATTRS = (
    "text", "photo", "voice", "audio", "video", "video_note", "sticker",
    "document", "animation", "poll", "contact", "location",
)

_NAME_MAX_LEN = 40
_FALLBACK_NAME = "Участник группы"

_allow_all: dict[int, bool] = {}  # owner_id -> разрешено всем участникам группы
_use_prefix: dict[int, bool] = {}  # owner_id -> имя автора для режима «всем»
_people: dict[int, dict[int, tuple[str, bool]]] = {}  # owner_id -> {user_id: (имя, с именем?)}
_last_notice: dict[tuple[int, int | None], float] = {}
_last_seen_write: dict[tuple[int, int], float] = {}
_last_attempt: dict[tuple[int, int], tuple[int, int | None, int]] = {}  # (owner, user) -> (чат, тема, сообщение)
_notify_times: dict[int, list[float]] = {}  # owner_id -> когда слали уведомления о новых людях
_NOTIFY_PER_HOUR = 5


async def init_sender_policy_table():
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sender_policy (
                owner_id INTEGER PRIMARY KEY,
                allow_all INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        try:
            await db.execute("ALTER TABLE sender_policy ADD COLUMN use_prefix INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass  # колонка уже есть
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sender_people (
                owner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                use_prefix INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (owner_id, user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sender_seen (
                owner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                seen_at INTEGER NOT NULL,
                PRIMARY KEY (owner_id, user_id)
            )
            """
        )
        # Откуда была последняя попытка написать: на это сообщение бот ответит,
        # когда человека разрешат.
        for column in ("chat_id", "thread_id", "message_id"):
            try:
                await db.execute(f"ALTER TABLE sender_seen ADD COLUMN {column} INTEGER")
            except Exception:
                pass  # колонка уже есть
        await db.commit()
        async with db.execute("SELECT owner_id, allow_all, use_prefix FROM sender_policy") as cur:
            policy_rows = await cur.fetchall()
        async with db.execute("SELECT owner_id, user_id, name, use_prefix FROM sender_people") as cur:
            people_rows = await cur.fetchall()
    _allow_all.clear()
    _use_prefix.clear()
    _people.clear()
    for owner_id, flag, prefix_flag in policy_rows:
        _allow_all[owner_id] = bool(flag)
        _use_prefix[owner_id] = bool(prefix_flag)
    for owner_id, user_id, name, prefix_flag in people_rows:
        _people.setdefault(owner_id, {})[user_id] = (name, bool(prefix_flag))


async def set_allow_all(owner_id: int, value: bool):
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO sender_policy (owner_id, allow_all) VALUES (?, ?)
            ON CONFLICT(owner_id) DO UPDATE SET allow_all = excluded.allow_all
            """,
            (owner_id, int(value)),
        )
        await db.commit()
    _allow_all[owner_id] = bool(value)


async def set_use_prefix(owner_id: int, value: bool):
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO sender_policy (owner_id, use_prefix) VALUES (?, ?)
            ON CONFLICT(owner_id) DO UPDATE SET use_prefix = excluded.use_prefix
            """,
            (owner_id, int(value)),
        )
        await db.commit()
    _use_prefix[owner_id] = bool(value)


async def add_person(owner_id: int, user_id: int, name: str, with_name: bool):
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO sender_people (owner_id, user_id, name, use_prefix) VALUES (?, ?, ?, ?)
            ON CONFLICT(owner_id, user_id) DO UPDATE SET name = excluded.name, use_prefix = excluded.use_prefix
            """,
            (owner_id, user_id, name, int(with_name)),
        )
        await db.execute("DELETE FROM sender_seen WHERE owner_id = ? AND user_id = ?", (owner_id, user_id))
        await db.commit()
    _people.setdefault(owner_id, {})[user_id] = (name, with_name)


async def set_person_prefix(owner_id: int, user_id: int, with_name: bool):
    person = _people.get(owner_id, {}).get(user_id)
    if person is None:
        return
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            "UPDATE sender_people SET use_prefix = ? WHERE owner_id = ? AND user_id = ?",
            (int(with_name), owner_id, user_id),
        )
        await db.commit()
    _people[owner_id][user_id] = (person[0], with_name)


async def remove_person(owner_id: int, user_id: int):
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute("DELETE FROM sender_people WHERE owner_id = ? AND user_id = ?", (owner_id, user_id))
        await db.commit()
    _people.get(owner_id, {}).pop(user_id, None)


async def remember_candidate(
    owner_id: int, user_id: int, name: str, target: tuple[int, int | None, int] | None = None
) -> bool:
    """Запоминает человека, чьё сообщение мы не переслали, чтобы владелец мог
    выбрать его кнопкой в /senders. target = (чат, тема, сообщение) последней
    попытки. Возвращает True, если видим его впервые (тогда владельцу стоит
    сообщить о попытке)."""
    chat_id, thread_id, message_id = target if target else (None, None, None)
    key = (owner_id, user_id)
    now = time.monotonic()
    if now - _last_seen_write.get(key, -_SEEN_WRITE_COOLDOWN) < _SEEN_WRITE_COOLDOWN:
        return False
    _last_seen_write[key] = now
    async with aiosqlite.connect(state.DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM sender_seen WHERE owner_id = ? AND user_id = ?", (owner_id, user_id)
        ) as cur:
            known = await cur.fetchone() is not None
        await db.execute(
            """
            INSERT INTO sender_seen (owner_id, user_id, name, seen_at, chat_id, thread_id, message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, user_id) DO UPDATE SET
                name = excluded.name, seen_at = excluded.seen_at,
                chat_id = COALESCE(excluded.chat_id, chat_id),
                thread_id = CASE WHEN excluded.chat_id IS NULL THEN thread_id ELSE excluded.thread_id END,
                message_id = COALESCE(excluded.message_id, message_id)
            """,
            (owner_id, user_id, name, int(time.time()), chat_id, thread_id, message_id),
        )
        await db.execute(
            """
            DELETE FROM sender_seen WHERE owner_id = ? AND user_id NOT IN (
                SELECT user_id FROM sender_seen WHERE owner_id = ? ORDER BY seen_at DESC LIMIT ?
            )
            """,
            (owner_id, owner_id, _MAX_CANDIDATES),
        )
        await db.commit()
    return not known


async def get_attempt_target(owner_id: int, user_id: int) -> tuple[int, int | None, int] | None:
    """Где человек пытался написать в последний раз: память (самое свежее),
    иначе то, что успели записать в базу."""
    target = _last_attempt.get((owner_id, user_id))
    if target is not None:
        return target
    async with aiosqlite.connect(state.DB_PATH) as db:
        async with db.execute(
            "SELECT chat_id, thread_id, message_id FROM sender_seen WHERE owner_id = ? AND user_id = ?",
            (owner_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    if row and row[0] is not None and row[2] is not None:
        return (row[0], row[1], row[2])
    return None


async def announce_allowed(name: str, target: tuple[int, int | None, int] | None):
    """Отвечает в группе на сообщение, которое не ушло, что теперь можно писать:
    само сообщение бот не хранит и не досылает, человеку нужно повторить."""
    if target is None:
        return
    chat_id, thread_id, message_id = target
    try:
        await state.tg_bot.send_message(
            chat_id=chat_id,
            text=f"✅ {name} теперь может писать в MAX. Отправьте сообщение ещё раз.",
            message_thread_id=thread_id,
            reply_parameters=ReplyParameters(message_id=message_id, allow_sending_without_reply=True),
        )
    except Exception as e:
        state.log.warning(f"Не удалось сообщить в группе {chat_id}, что {name} разрешён: {e}")


async def list_candidates(owner_id: int) -> list[tuple[int, str]]:
    async with aiosqlite.connect(state.DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, name FROM sender_seen WHERE owner_id = ? ORDER BY seen_at DESC LIMIT ?",
            (owner_id, _MAX_CANDIDATES),
        ) as cur:
            return [(user_id, name) for user_id, name in await cur.fetchall()]


async def reset(owner_id: int):
    """Возвращает безопасное «только владелец» (например, после /logout)."""
    _allow_all.pop(owner_id, None)
    _use_prefix.pop(owner_id, None)
    _people.pop(owner_id, None)
    for key in [k for k in _last_seen_write if k[0] == owner_id]:
        _last_seen_write.pop(key, None)
    _notify_times.pop(owner_id, None)
    for key in [k for k in _last_attempt if k[0] == owner_id]:
        _last_attempt.pop(key, None)
    try:
        async with aiosqlite.connect(state.DB_PATH) as db:
            for table in ("sender_policy", "sender_people", "sender_seen"):
                await db.execute(f"DELETE FROM {table} WHERE owner_id = ?", (owner_id,))
            await db.commit()
    except Exception as e:
        state.log.warning(f"Не удалось сбросить настройку отправителей для {owner_id}: {e}")


def allow_all(owner_id: int) -> bool:
    return _allow_all.get(owner_id, False)


def use_prefix(owner_id: int) -> bool:
    return _use_prefix.get(owner_id, True)


def people(owner_id: int) -> dict[int, tuple[str, bool]]:
    return _people.get(owner_id, {})


def owner_of_chat(chat_id: int) -> int | None:
    """Чей мост обслуживает эта TG-группа: админский или tenant'а."""
    if chat_id in state.allowed_groups_runtime:
        return ADMIN_ID
    return state.tenant_group_map.get(chat_id)


def _actor_is_owner(owner_id: int, chat_id: int, user, sender_chat) -> bool:
    if user is not None and user.id == owner_id:
        return True
    # Анонимный админ: вместо автора Telegram ставит служебного GroupAnonymousBot,
    # а в sender_chat лежит сама группа. Админов группы назначает владелец.
    return sender_chat is not None and sender_chat.id == chat_id


def _person(owner_id: int, user) -> tuple[str, bool] | None:
    if user is None:
        return None
    return people(owner_id).get(user.id)


def _allowed(chat_id: int, user, sender_chat) -> bool:
    owner_id = owner_of_chat(chat_id)
    if owner_id is None or allow_all(owner_id):
        return True
    if _actor_is_owner(owner_id, chat_id, user, sender_chat):
        return True
    return _person(owner_id, user) is not None


def message_allowed(message: TgMessage) -> bool:
    return _allowed(
        message.chat.id,
        getattr(message, "from_user", None),
        getattr(message, "sender_chat", None),
    )


def reaction_allowed(reaction) -> bool:
    return _allowed(
        reaction.chat.id,
        getattr(reaction, "user", None),
        getattr(reaction, "actor_chat", None),
    )


def _author_name(user, sender_chat) -> str:
    name = ""
    if user is not None:
        name = (
            getattr(user, "full_name", None)
            or getattr(user, "first_name", None)
            or getattr(user, "username", None)
            or ""
        )
    if not name and sender_chat is not None:
        name = getattr(sender_chat, "title", None) or ""
    # Звёздочки, подчёркивания и т.п. MAX разберёт как разметку и исказит имя.
    name = re.sub(r"[*_`~\[\]\n]", "", str(name)).strip()[:_NAME_MAX_LEN].strip()
    return name or _FALLBACK_NAME


def author_prefix(message: TgMessage) -> str:
    """Жирное имя отдельной строкой («**Иван**\\n») для сообщения чужого
    участника, если для него включено имя; пустая строка для владельца и во
    всех остальных случаях."""
    chat_id = message.chat.id
    owner_id = owner_of_chat(chat_id)
    if owner_id is None:
        return ""
    user = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)
    if _actor_is_owner(owner_id, chat_id, user, sender_chat):
        return ""
    person = _person(owner_id, user)
    if person is not None:
        with_name = person[1]
    elif allow_all(owner_id):
        with_name = use_prefix(owner_id)
    else:
        return ""
    # Жирное имя отдельной строкой, как Telegram показывает автора; MAX разбирает
    # **...** сам (и при отправке, и при правке).
    return f"**{_author_name(user, sender_chat)}**\n" if with_name else ""


def with_prefix(prefix: str, text: str) -> str:
    if not prefix:
        return text
    return f"{prefix}{text}" if text else prefix.rstrip()


def _has_payload(message: TgMessage) -> bool:
    """Служебные события (вход в группу, создание темы) не считаем попыткой
    написать в MAX — на них отвечать не нужно."""
    return any(getattr(message, attr, None) for attr in _PAYLOAD_ATTRS)


async def _remember_author(message: TgMessage) -> tuple[int, int, str] | None:
    """Запоминает автора; возвращает (owner_id, user_id, имя), если это новый
    человек и владельцу стоит сообщить."""
    user = getattr(message, "from_user", None)
    if user is None or getattr(user, "is_bot", False) or getattr(message, "sender_chat", None) is not None:
        return None
    owner_id = owner_of_chat(message.chat.id)
    if owner_id is None:
        return None
    name = _author_name(user, None)
    target = (message.chat.id, getattr(message, "message_thread_id", None), message.message_id)
    _last_attempt[(owner_id, user.id)] = target
    try:
        is_new = await remember_candidate(owner_id, user.id, name, target)
    except Exception as e:
        state.log.warning(f"Не удалось запомнить автора {user.id} для {owner_id}: {e}")
        return None
    return (owner_id, user.id, name) if is_new else None


def _notify_budget_left(owner_id: int) -> bool:
    """Не больше _NOTIFY_PER_HOUR уведомлений в час, чтобы людная группа не
    завалила владельца (остальные всё равно попадают в список /senders)."""
    now = time.monotonic()
    recent = [t for t in _notify_times.get(owner_id, []) if now - t < 3600]
    if len(recent) >= _NOTIFY_PER_HOUR:
        _notify_times[owner_id] = recent
        return False
    recent.append(now)
    _notify_times[owner_id] = recent
    return True


def _notice_keyboard(owner_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Разрешить с именем", callback_data=f"senders:allow1:{owner_id}:{user_id}")],
        [InlineKeyboardButton(text="Разрешить без имени", callback_data=f"senders:allow0:{owner_id}:{user_id}")],
        [InlineKeyboardButton(text="Не сейчас", callback_data=f"senders:dismiss:{owner_id}:{user_id}")],
    ])


async def _notify_owner(message: TgMessage, owner_id: int, user_id: int, name: str):
    """Сообщает владельцу в личку, что новый человек пытался написать, и даёт
    кнопки, чтобы сразу разрешить его. Текст сообщения в уведомление не
    попадает."""
    if not _notify_budget_left(owner_id):
        return
    group = getattr(message.chat, "title", None)
    where = f" в группе «{_esc(group)}»" if group else " в группе"
    shown = _esc(name)
    text = (
        f"👤 <b>{shown}</b> пытался написать{where}, но сообщение в MAX не ушло: "
        "туда пересылаются только твои сообщения.\n\n"
        "Разрешить ему писать в MAX от твоего имени? Так это увидят в MAX:\n"
        f"• с именем: <b>{shown}</b> жирной строкой, а под ней текст\n"
        "• без имени: просто текст\n\n"
        "Если разрешишь, бот ответит ему в группе, что можно написать ещё раз (само сообщение "
        "бот не хранит). Если нет, ничего не делай: сообщения этого человека пересылаться не будут."
    )
    try:
        await state.tg_bot.send_message(
            owner_id, text, parse_mode="HTML", reply_markup=_notice_keyboard(owner_id, user_id)
        )
    except Exception as e:
        state.log.warning(f"Не удалось сообщить владельцу {owner_id} о попытке {user_id}: {e}")


async def notify_rejected(message: TgMessage):
    """Запоминает автора (чтобы владелец мог его разрешить), сообщает владельцу
    о новом человеке и один раз в 10 минут на тему объясняет, почему сообщение
    не ушло в MAX, чтобы человек не думал, что мост сломался."""
    if not _has_payload(message):
        return
    fresh = await _remember_author(message)
    if fresh is not None:
        await _notify_owner(message, *fresh)
    key = (message.chat.id, getattr(message, "message_thread_id", None))
    now = time.monotonic()
    if now - _last_notice.get(key, -_NOTICE_COOLDOWN) < _NOTICE_COOLDOWN:
        return
    _last_notice[key] = now
    try:
        await message.reply(
            "🔒 В MAX пересылаются только сообщения владельца моста, а ваше туда не ушло.\n"
            "Если нужно, владелец может разрешить вам писать."
        )
    except Exception as e:
        state.log.warning(f"Не удалось объяснить отказ в группе {message.chat.id}: {e}")


# --- /senders: настройки в личке ---

_PERSON_ACTIONS = ("pick", "allow1", "allow0", "person", "up1", "up0", "rm", "dismiss")
_ACTIONS = ("all", "owner", "pfxon", "pfxoff", "add", "main") + _PERSON_ACTIONS


def _is_senders_command(message: TgMessage) -> bool:
    if message.chat.type != "private" or message.from_user is None or not message.text:
        return False
    parts = message.text.strip().split()
    return bool(parts) and parts[0].split("@")[0] == "/senders"


def _esc(name: str) -> str:
    return html.escape(name, quote=False)


def _status_text(allow: bool, prefix: bool = True, named: dict[int, tuple[str, bool]] | None = None) -> str:
    named = named or {}
    if allow:
        head = (
            "⚠️ Сейчас в MAX уходят сообщения, правки и реакции <b>всех участников</b> группы, "
            "от твоего имени.\n\n"
        )
        if prefix:
            body = (
                "К сообщениям других участников бот добавляет имя автора жирной строкой над "
                "текстом, твои идут без имени. Реакции имени не несут: в MAX их не отличить от твоих."
            )
        else:
            body = (
                "Имена авторов не добавляются, в MAX всё выглядит так, будто написал ты. "
                "Это удобно, если аккаунтом пользуются несколько близких людей; если нет, "
                "лучше включить имена."
            )
    elif named:
        head = "🔒 "
        body = (
            "Сейчас в MAX уходят сообщения, правки и реакции <b>твои</b> и людей из списка ниже. "
            "Остальных участников группы бот не пересылает."
        )
    else:
        head = "🔒 "
        body = (
            "Сейчас в MAX уходят только <b>твои</b> сообщения, правки и реакции из группы. "
            "Всё, что делают другие участники группы, бот не пересылает.\n\n"
            "Можно разрешить конкретного человека (кнопка «Добавить человека») или сразу всех "
            "участников группы."
        )
    text = head + body
    if named:
        lines = [
            f"• {_esc(name)}: {'с именем' if with_name else 'без имени'}"
            for name, with_name in named.values()
        ]
        text += "\n\n<b>Разрешены отдельно:</b>\n" + "\n".join(lines)
    return text


def _keyboard(
    owner_id: int,
    allow: bool,
    prefix: bool = True,
    named: dict[int, tuple[str, bool]] | None = None,
) -> InlineKeyboardMarkup:
    rows = []
    if allow:
        rows.append([InlineKeyboardButton(text="🔒 Только я", callback_data=f"senders:owner:{owner_id}")])
        if prefix:
            rows.append([InlineKeyboardButton(
                text="✏️ Имена авторов: вкл (выключить)", callback_data=f"senders:pfxoff:{owner_id}")])
        else:
            rows.append([InlineKeyboardButton(
                text="✏️ Имена авторов: выкл (включить)", callback_data=f"senders:pfxon:{owner_id}")])
    else:
        rows.append([InlineKeyboardButton(
            text="👥 Разрешить всем участникам", callback_data=f"senders:all:{owner_id}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить человека", callback_data=f"senders:add:{owner_id}")])
    for user_id, (name, with_name) in (named or {}).items():
        rows.append([InlineKeyboardButton(
            text=f"👤 {name} ({'с именем' if with_name else 'без имени'})",
            callback_data=f"senders:person:{owner_id}:{user_id}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _screen(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    allow, prefix, named = allow_all(owner_id), use_prefix(owner_id), people(owner_id)
    return _status_text(allow, prefix, named), _keyboard(owner_id, allow, prefix, named)


def _back_row(owner_id: int, target: str = "main") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="◀️ Назад", callback_data=f"senders:{target}:{owner_id}")]


async def _add_screen(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    already = people(owner_id)
    candidates = [(uid, name) for uid, name in await list_candidates(owner_id) if uid not in already]
    if not candidates:
        text = (
            "Пока никто не пытался писать в группу. Список участников Telegram боту не отдаёт, "
            "поэтому бот запоминает тех, кто написал сам.\n\n"
            "Попроси нужного человека написать что-нибудь в группе (бот ему ответит, что сообщение "
            "не ушло), потом открой это меню снова."
        )
        return text, InlineKeyboardMarkup(inline_keyboard=[_back_row(owner_id)])
    text = (
        "Кого разрешить? Это люди, которые недавно пытались написать в группу. "
        "Если нужного нет, попроси его написать что-нибудь в группе и открой это меню снова."
    )
    rows = [
        [InlineKeyboardButton(text=f"👤 {name}", callback_data=f"senders:pick:{owner_id}:{uid}")]
        for uid, name in candidates
    ]
    rows.append(_back_row(owner_id))
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _pick_screen(owner_id: int, user_id: int, name: str) -> tuple[str, InlineKeyboardMarkup]:
    shown = _esc(name)
    text = (
        f"Разрешить <b>{shown}</b> писать в MAX от твоего имени (сообщения, правки и реакции).\n\n"
        "Добавлять его имя к сообщениям? Так это увидят в MAX:\n"
        f"• с именем: <b>{shown}</b> жирной строкой, а под ней «привет»\n"
        "• без имени: просто «привет»"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ С именем", callback_data=f"senders:allow1:{owner_id}:{user_id}")],
        [InlineKeyboardButton(text="Без имени", callback_data=f"senders:allow0:{owner_id}:{user_id}")],
        _back_row(owner_id, "add"),
    ])
    return text, markup


def _person_screen(owner_id: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    name, with_name = people(owner_id)[user_id]
    shown = _esc(name)
    text = (
        f"<b>{shown}</b> может писать в MAX от твоего имени.\n"
        + (f"Сообщения идут с именем: <b>{shown}</b> жирной строкой, а под ней текст." if with_name
           else "Сообщения идут без имени, просто текстом.")
    )
    if with_name:
        toggle = InlineKeyboardButton(text="Убрать имя", callback_data=f"senders:up0:{owner_id}:{user_id}")
    else:
        toggle = InlineKeyboardButton(text="✏️ Добавлять имя", callback_data=f"senders:up1:{owner_id}:{user_id}")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [toggle],
        [InlineKeyboardButton(text="🗑 Убрать из списка", callback_data=f"senders:rm:{owner_id}:{user_id}")],
        _back_row(owner_id),
    ])
    return text, markup


async def _resolve_owner(user_id: int) -> int | None:
    """Настройка доступна админу и тем, кто уже подключал MAX-аккаунт."""
    if user_id == ADMIN_ID:
        return ADMIN_ID
    import tenants  # отложенный импорт: tenants сам импортирует этот модуль
    tenant = await tenants._get_tenant(user_id)
    if tenant and tenant["status"] != "awaiting_phone":
        return user_id
    return None


@state.dp.message(_is_senders_command)
async def cmd_senders(message: TgMessage):
    owner_id = await _resolve_owner(message.from_user.id)
    if owner_id is None:
        await message.reply("Эта настройка появится, когда подключишь MAX-аккаунт. Начни с /start.")
        return
    text, markup = _screen(owner_id)
    await message.reply(text, parse_mode="HTML", reply_markup=markup)


async def _show(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    if not query.message:
        return
    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        pass  # то же состояние нажали повторно: Telegram не даёт «изменить» на то же самое


@state.dp.callback_query(lambda c: c.data and c.data.startswith("senders:"))
async def cb_senders(query: CallbackQuery):
    parts = (query.data or "").split(":")
    if len(parts) not in (3, 4) or parts[1] not in _ACTIONS:
        await query.answer("Ошибка данных")
        return
    action = parts[1]
    needs_user = action in _PERSON_ACTIONS
    if needs_user != (len(parts) == 4):
        await query.answer("Ошибка данных")
        return
    try:
        owner_id = int(parts[2])
        user_id = int(parts[3]) if needs_user else None
    except ValueError:
        await query.answer("Плохой id")
        return

    if not query.from_user or query.from_user.id != owner_id:
        await query.answer("Это не твоя кнопка", show_alert=True)
        return
    if await _resolve_owner(owner_id) is None:
        await query.answer("Сейчас это недоступно", show_alert=True)
        return

    try:
        if action in ("all", "owner"):
            await set_allow_all(owner_id, action == "all")
        elif action in ("pfxon", "pfxoff"):
            await set_use_prefix(owner_id, action == "pfxon")
        elif action == "add":
            await query.answer()
            text, markup = await _add_screen(owner_id)
            await _show(query, text, markup)
            return
        elif action == "main":
            await query.answer()
            await _show(query, *_screen(owner_id))
            return
        elif action in ("pick", "allow1", "allow0"):
            name = next((n for uid, n in await list_candidates(owner_id) if uid == user_id), None)
            if name is None:
                await query.answer("Этого человека нет в списке: пусть напишет в группе ещё раз", show_alert=True)
                text, markup = await _add_screen(owner_id)
                await _show(query, text, markup)
                return
            if action == "pick":
                await query.answer()
                text, markup = _pick_screen(owner_id, user_id, name)
                await _show(query, text, markup)
                return
            if len(people(owner_id)) >= _MAX_PEOPLE and user_id not in people(owner_id):
                await query.answer(f"Список полон ({_MAX_PEOPLE} человек)", show_alert=True)
                return
            target = await get_attempt_target(owner_id, user_id)
            await add_person(owner_id, user_id, name, action == "allow1")
            _last_attempt.pop((owner_id, user_id), None)
            await announce_allowed(name, target)
        elif action == "person":
            await query.answer()
            if user_id not in people(owner_id):
                await _show(query, *_screen(owner_id))
                return
            await _show(query, *_person_screen(owner_id, user_id))
            return
        elif action in ("up1", "up0"):
            await set_person_prefix(owner_id, user_id, action == "up1")
            await query.answer("Готово")
            if user_id in people(owner_id):
                await _show(query, *_person_screen(owner_id, user_id))
            return
        elif action == "rm":
            await remove_person(owner_id, user_id)
        elif action == "dismiss":
            await query.answer("Хорошо")
            if query.message:
                try:
                    await query.message.edit_text(
                        "Хорошо, ничего не меняю. Если передумаешь: /senders → «Добавить человека»."
                    )
                except Exception:
                    pass
            return
    except Exception as e:
        state.log.warning(f"Не удалось сохранить настройку отправителей для {owner_id}: {e}")
        await query.answer("Не удалось сохранить, попробуй ещё раз", show_alert=True)
        return

    await query.answer("Готово")
    await _show(query, *_screen(owner_id))
