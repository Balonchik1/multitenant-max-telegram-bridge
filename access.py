import logging
import aiosqlite

from aiogram import Dispatcher, Bot
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message as TgMessage,
)
from aiogram.dispatcher.event.bases import SkipHandler

from config import REQUIRE_APPROVAL
from formatters import safe_html

log = logging.getLogger("bridge.access")

# заполняется в setup_access()
_db_path: str = "bridge.db"
_admin_id: int = 0
_bot: Bot | None = None
# message_id у админа → tg_id пользователя
_admin_msg_to_user: dict[int, int] = {}
# tg_id людей, которые написали "/support" без текста и должны прислать
# сообщение следующим — их следующее НЕ-командное сообщение уходит админу.
_awaiting_support: set[int] = set()

async def init_access_table(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS access_users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                status TEXT NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        await db.commit()

async def get_access_status(tg_id: int) -> str | None:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT status FROM access_users WHERE tg_id = ?",
            (tg_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def upsert_access(
    tg_id: int,
    status: str,
    username: str | None = None,
    full_name: str | None = None,
):
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO access_users (tg_id, username, full_name, status, updated_at)
            VALUES (?, ?, ?, ?, strftime('%s', 'now'))
            ON CONFLICT(tg_id) DO UPDATE SET
                status = excluded.status,
                username = COALESCE(excluded.username, access_users.username),
                full_name = COALESCE(excluded.full_name, access_users.full_name),
                updated_at = strftime('%s', 'now')
            """,
            (tg_id, username, full_name, status),
        )
        await db.commit()

def _user_meta(message: TgMessage) -> tuple[int, str | None, str]:
    u = message.from_user
    uid = u.id
    username = u.username
    full_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or str(uid)
    return uid, username, full_name


async def _notify_admin_new_user(uid: int, username: str | None, full_name: str):
    if not _bot:
        return
    uname = f"@{username}" if username else "—"
    try:
        await _bot.send_message(
            _admin_id,
            (
                f"🆕 <b>Новый пользователь начал регистрацию</b>\n\n"
                f"Имя: <b>{safe_html(full_name)}</b>\n"
                f"Username: {safe_html(uname)}\n"
                f"id: <code>{uid}</code>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning(f"notify admin new user: {e}")


def _approval_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Разрешить", callback_data=f"access:allow:{uid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"access:deny:{uid}"),
    ]])


async def _request_approval(uid: int, username: str | None, full_name: str) -> None:
    """Незнакомый человек написал боту при включённом REQUIRE_APPROVAL: ставим
    ему статус pending и отправляем админу заявку с кнопками."""
    await upsert_access(uid, "pending", username, full_name)
    if not _bot:
        return
    uname = f"@{username}" if username else "—"
    try:
        await _bot.send_message(
            _admin_id,
            (
                f"🆕 <b>Заявка на подключение</b>\n\n"
                f"Имя: <b>{safe_html(full_name)}</b>\n"
                f"Username: {safe_html(uname)}\n"
                f"id: <code>{uid}</code>"
            ),
            parse_mode="HTML",
            reply_markup=_approval_keyboard(uid),
        )
    except Exception as e:
        log.warning(f"notify admin approval request: {e}")


_INTRO_TEXT = (
    "Это мост между MAX и Telegram: переписка из MAX приходит сюда отдельными темами "
    "в твоей Telegram-группе, а ответы отсюда уходят обратно в MAX.\n\n"
    "Технически для этого боту нужен полный доступ к твоему аккаунту MAX (как у самого приложения). "
    "Рекомендую подключать аккаунт без важной переписки и без привязанных Госуслуг. "
    "Доступ можно отозвать в любой момент: командой /logout или в приложении MAX, "
    "завершив сессию бота в списке устройств.\n\n"
    "Доступ выдаёт администратор. Нажми кнопку, если хочешь отправить заявку."
)
async def _revoke_connected(target_id: int) -> str | None:
    """Если у запрещаемого человека есть подключение (или незавершённый вход),
    отключает его и возвращает пояснение для админа; иначе None."""
    try:
        import tenants  # отложенный импорт — рвём цикл access<->tenants<->state
        result = await tenants.revoke_tenant(target_id)
    except Exception as e:
        log.warning(f"revoke on deny {target_id}: {e}")
        return f"⚠️ Отключить подключение не получилось: {safe_html(str(e))}"
    if result is None:
        return None
    if not result["was_logged_in"]:
        return "Незавершённый вход отменён, данные удалены."
    if result["server_logout"] is True:
        return "Подключение отключено: сессия в MAX завершена, данные входа удалены."
    if result["server_logout"] is False:
        return "Подключение отключено: запрос на выход в MAX отправлен, данные входа удалены."
    return (
        "Подключение отключено, данные входа удалены, но живого соединения с MAX не было, "
        "поэтому сессию завершить не удалось. Проверь список устройств в MAX этого аккаунта."
    )


_PENDING_REPLY ="✅ Заявка отправлена администратору, жди решения. Как только её одобрят, я напишу."
_PENDING_AGAIN_REPLY = "Твоя заявка уже на рассмотрении у администратора, жди решения."


def _request_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отправить заявку", callback_data=f"access:request:{uid}"),
    ]])


async def _forward_support_message(uid: int, username: str | None, full_name: str, body: str) -> bool:
    if not _bot:
        return False
    uname = f"@{username}" if username else "—"
    try:
        msg = await _bot.send_message(
            _admin_id,
            (
                f"💬 <b>/support от</b> {safe_html(full_name)} ({safe_html(uname)}) "
                f"<code>{uid}</code>\n\n{safe_html(body)}\n\n"
                f"<i>Reply на это сообщение = ответ пользователю</i>"
            ),
            parse_mode="HTML",
        )
        _admin_msg_to_user[msg.message_id] = uid
        return True
    except Exception as e:
        log.warning(f"support forward: {e}")
        return False


def setup_access(dp: Dispatcher, bot: Bot, db_path: str, admin_id: int):
    global _db_path, _admin_id, _bot
    _db_path = db_path
    _admin_id = int(admin_id)
    _bot = bot

    @dp.message(lambda m: m.text and m.text.startswith("/start"))
    async def cmd_start(message: TgMessage):
        if not message.from_user:
            return
        uid, username, full_name = _user_meta(message)

        if uid == _admin_id:
            await message.reply(
                "Привет, админ.\nКоманды моста: /ping /alias /chats /groups /bind …"
            )
            return

        status = await get_access_status(uid)

        if status == "denied":
            await message.reply(
                "Бот пока в разработке.\n"
                "Можешь написать сообщение сюда — создатель его увидит."
            )
            return

        if REQUIRE_APPROVAL and status == "pending":
            await message.reply(_PENDING_AGAIN_REPLY)
            return

        if REQUIRE_APPROVAL and status is None:
            await message.reply(_INTRO_TEXT, reply_markup=_request_keyboard(uid))
            return

        if status != "allowed":
            await upsert_access(uid, "allowed", username, full_name)
            await _notify_admin_new_user(uid, username, full_name)
            try:
                import tenants  # отложенный импорт — рвём цикл access<->tenants<->state
                await tenants.start_onboarding(uid)
            except Exception as e:
                log.warning(f"start -> start_onboarding: {e}")
            return

        # Уже allowed — говорим, на каком этапе, вместо тишины (текст "/start"
        # сам по себе ничего не значит для tenants.py, дальше передавать не нужно).
        try:
            import tenants  # отложенный импорт — рвём цикл access<->tenants<->state
            tenant = await tenants._get_tenant(uid)
        except Exception:
            tenant = None

        status_text = {
            "awaiting_phone": "Жду от тебя номер телефона MAX.",
            "awaiting_login": "Сейчас пытаюсь войти в твой MAX-аккаунт — если просил код или пароль, пришли его сюда.",
            "needs_2fa": "Нужно включить облачный пароль в MAX, потом напиши /retry.",
            "awaiting_group": "Вход выполнен, жду пока привяжешь группу с темами и напишешь там /setupgroup (или /retry, если бот перезапускался).",
            "active": "Уже всё подключено и работает. Если что-то не так — напиши /support.",
            "failed": "Прошлая попытка входа не удалась — напиши /retry.",
        }.get(tenant["status"] if tenant else None, "Уже в процессе подключения — если что-то неясно, напиши /support.")

        await message.reply(status_text)

    @dp.message(lambda m: m.chat.type == "private" and m.from_user and m.from_user.id != _admin_id)
    async def private_non_admin(message: TgMessage):
        uid, username, full_name = _user_meta(message)

        if uid in _awaiting_support:
            _awaiting_support.discard(uid)
            text = message.text or ""
            if text.startswith("/"):
                # прислал команду вместо текста — не пересылаем, обрабатываем как обычно
                raise SkipHandler()
            body = message.text or message.caption or "(не текст / медиа)"
            ok = await _forward_support_message(uid, username, full_name, body)
            await message.reply("✅ Сообщение отправлено создателю." if ok else "Не получилось отправить, попробуй позже.")
            return

        if message.text and message.text.startswith("/start"):
            raise SkipHandler()

        status = await get_access_status(uid)

        if REQUIRE_APPROVAL and status == "pending":
            if (message.text or "").startswith("/support"):
                raise SkipHandler()  # пусть сможет написать админу, пока ждёт решения
            await message.reply(_PENDING_AGAIN_REPLY)
            return

        if REQUIRE_APPROVAL and status is None:
            await message.reply(_INTRO_TEXT, reply_markup=_request_keyboard(uid))
            return

        if status is None:
            await upsert_access(uid, "allowed", username, full_name)
            await _notify_admin_new_user(uid, username, full_name)
            try:
                import tenants  # отложенный импорт — рвём цикл access<->tenants<->state
                await tenants.start_onboarding(uid)
            except Exception as e:
                log.warning(f"first contact -> start_onboarding: {e}")
            return

        if status == "allowed":
            # Дальше уже онбординг/маршрутизация из tenants.py — не глушим тут.
            raise SkipHandler()

        # status == "denied" — остаётся как канал связи с админом (бан всё ещё
        # можно выдать вручную через /deny, и такой человек может написать админу).
        text = message.text or message.caption or "(не текст / медиа)"
        uname = f"@{username}" if username else "—"
        try:
            msg = await bot.send_message(
                _admin_id,
                (
                    f"💬 <b>От</b> {safe_html(full_name)} ({safe_html(uname)}) "
                    f"<code>{uid}</code> [{status}]\n\n{safe_html(text)}\n\n"
                    f"<i>Reply на это сообщение = ответ пользователю</i>"
                ),
                parse_mode="HTML",
            )
            _admin_msg_to_user[msg.message_id] = uid
        except Exception as e:
            log.warning(f"forward to admin: {e}")

        await message.reply("Бот пока в разработке.\nСообщение передано создателю.")
    @dp.message(lambda m: m.chat.type == "private" and m.from_user and m.from_user.id == _admin_id)
    async def admin_reply_to_user(message: TgMessage):
        # не перехватывать auth и команды
        if message.text and message.text.startswith("/"):
            raise SkipHandler()

        if not message.reply_to_message:
            raise SkipHandler()

        reply_id = message.reply_to_message.message_id
        target_id = _admin_msg_to_user.get(reply_id)
        if not target_id:
            # на случай перезапуска — id из текста исходного сообщения
            src = message.reply_to_message.text or message.reply_to_message.caption or ""
            import re
            m = re.search(r"id: (\d+)", src) or re.search(r"\b(\d{6,})\b", src)
            if m:
                target_id = int(m.group(1))
            else:
                raise SkipHandler()

        body = message.text or message.caption
        if not body:
            await message.reply("Можно ответить текстом.")
            return

        try:
            await bot.send_message(target_id, f"✉️ Сообщение от создателя:\n\n{body}")
            await message.reply("✅ Отправлено")
        except Exception as e:
            await message.reply(f"Не удалось отправить: {e}")
    @dp.message(lambda m: m.text and m.text.startswith("/support"))
    async def cmd_support(message: TgMessage):
        if not message.from_user or message.from_user.id == _admin_id:
            return

        uid, username, full_name = _user_meta(message)
        body = (message.text or "")[len("/support"):].strip()

        if not body:
            # Двухшаговый вариант: /support без текста -> просим написать следующим
            # сообщением. Если это следующее сообщение окажется командой (например,
            # случайно ещё раз /support) — private_non_admin его не перешлёт.
            _awaiting_support.add(uid)
            await message.reply("Напиши следующим сообщением, что хочешь передать создателю.")
            return

        # Одношаговый вариант: /support <текст> сразу.
        ok = await _forward_support_message(uid, username, full_name, body)
        await message.reply("✅ Сообщение отправлено создателю." if ok else "Не получилось отправить, попробуй позже.")

    @dp.message(lambda m: m.text and m.text.startswith("/access"))
    async def cmd_access(message: TgMessage):
        if not message.from_user or message.from_user.id != _admin_id:
            return

        async with aiosqlite.connect(_db_path) as db:
            async with db.execute(
                """
                SELECT tg_id, username, full_name, status
                FROM access_users
                ORDER BY
                    CASE status
                        WHEN 'pending' THEN 0
                        WHEN 'allowed' THEN 1
                        ELSE 2
                    END,
                    updated_at DESC
                """
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await message.reply("Список пуст.\n/allow <id> — разрешить\n/deny <id> — запретить")
            return

        lines = []
        for tg_id, username, full_name, status in rows:
            uname = f"@{username}" if username else "—"
            name = full_name or "—"
            mark = {"pending": "⏳", "allowed": "✅", "denied": "❌"}.get(status, status)
            lines.append(
                f"{mark} <code>{tg_id}</code> {safe_html(name)} ({safe_html(uname)}) — <b>{status}</b>"
            )

        text = "<b>Доступ:</b>\n" + "\n".join(lines)
        text += "\n\n<code>/allow id</code> · <code>/deny id</code>"
        if len(text) > 4000:
            text = text[:4000] + "\n…"
        await message.reply(text, parse_mode="HTML")

    @dp.message(lambda m: m.text and m.text.startswith("/allow"))
    async def cmd_allow(message: TgMessage):
        if not message.from_user or message.from_user.id != _admin_id:
            return

        parts = (message.text or "").strip().split()
        if len(parts) != 2:
            await message.reply("Формат: /allow <tg_id>")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("id должен быть числом")
            return

        await upsert_access(target_id, "allowed")
        await message.reply(f"✅ Разрешён <code>{target_id}</code>", parse_mode="HTML")
        try:
            import tenants  # отложенный импорт — рвём цикл access<->tenants<->state
            await tenants.start_onboarding(target_id)
        except Exception as e:
            await message.reply(f"(онбординг не запустился: {e})")

    @dp.message(lambda m: m.text and m.text.startswith("/deny"))
    async def cmd_deny(message: TgMessage):
        if not message.from_user or message.from_user.id != _admin_id:
            return

        parts = (message.text or "").strip().split()
        if len(parts) != 2:
            await message.reply("Формат: /deny <tg_id>")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("id должен быть числом")
            return

        await upsert_access(target_id, "denied")
        revoke_note = await _revoke_connected(target_id)
        await message.reply(
            f"❌ Запрещён <code>{target_id}</code>" + (f"\n{revoke_note}" if revoke_note else ""),
            parse_mode="HTML",
        )
        if revoke_note:
            return  # человека уже предупредил revoke_tenant, общий отказ был бы лишним
        try:
            await bot.send_message(
                target_id,
                "Бот пока в разработке.\n"
                "Для связи напиши сюда сообщение — создатель его увидит.",
            )
        except Exception as e:
            await message.reply(f"(пользователю не написалось: {e})")

    @dp.callback_query(lambda c: c.data and c.data.startswith("access:"))
    async def cb_access(query: CallbackQuery):
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[1] not in ("request", "allow", "deny"):
            await query.answer("Ошибка данных")
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            await query.answer("Плохой id")
            return

        if parts[1] == "request":
            # Кнопка «Отправить заявку» под вводным текстом — нажимает сам человек.
            if not query.from_user or query.from_user.id != target_id:
                await query.answer("Это не твоя кнопка", show_alert=True)
                return
            status = await get_access_status(target_id)
            if status == "pending":
                await query.answer("Заявка уже на рассмотрении")
                return
            if status is not None:
                await query.answer("Сейчас заявку отправить нельзя")
                return
            u = query.from_user
            full_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or str(target_id)
            await _request_approval(target_id, u.username, full_name)
            await query.answer("Заявка отправлена")
            if query.message:
                try:
                    await query.message.edit_text(_PENDING_REPLY)
                except Exception:
                    pass
            return

        if not query.from_user or query.from_user.id != _admin_id:
            await query.answer("Только админ может решать", show_alert=True)
            return

        async def _edit(text: str):
            if query.message:
                try:
                    await query.message.edit_text(text, parse_mode="HTML")
                except Exception:
                    pass

        if parts[1] == "allow":
            await upsert_access(target_id, "allowed")
            await query.answer("Разрешён")
            await _edit(f"✅ Разрешён <code>{target_id}</code>")
            try:
                await bot.send_message(target_id, "✅ Администратор одобрил твою заявку.")
            except Exception as e:
                log.warning(f"approve notify {target_id}: {e}")
            try:
                import tenants  # отложенный импорт — рвём цикл access<->tenants<->state
                await tenants.start_onboarding(target_id)
            except Exception as e:
                await _edit(f"✅ Разрешён <code>{target_id}</code> (онбординг не запустился: {safe_html(str(e))})")
        else:
            await upsert_access(target_id, "denied")
            revoke_note = await _revoke_connected(target_id)
            await query.answer("Отклонён")
            await _edit(f"❌ Отклонён <code>{target_id}</code>" + (f"\n{revoke_note}" if revoke_note else ""))
            if revoke_note:
                return
            try:
                await bot.send_message(
                    target_id,
                    "Заявка отклонена.\n"
                    "Если это ошибка, напиши сюда сообщение — создатель его увидит.",
                )
            except Exception as e:
                log.warning(f"deny notify {target_id}: {e}")