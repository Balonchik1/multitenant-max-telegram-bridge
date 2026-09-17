"""Админские слэш-команды бота (/groups, /link, /ping, /alias, /chats, ...)."""
import html
import subprocess
import sys
import time

import aiosqlite
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message as TgMessage

from config import ADMIN_ID, TG_GROUP_ID, TG_GROUP_FLAT
from formatters import safe_html
from db_meta import (
    list_pings,
    set_ping,
    delete_ping,
    list_aliases,
    set_alias,
    delete_alias,
    list_mention_links,
    set_mention_link,
    delete_mention_link,
)
from db_messages import save_thread, get_dialog_by_thread
import state


@state.dp.message(lambda m: m.text and m.text.startswith("/info"))
async def cmd_info(message: TgMessage):
    """Повторно постит инструкцию по командам в #General группы — не
    привязано к тому, отправлялась ли она уже раньше (по просьбе: полезно,
    например, когда в группу зашёл новый человек)."""
    chat_id = message.chat.id
    is_admin_group = chat_id in state.allowed_groups_runtime
    is_tenant_group = chat_id in state.tenant_group_map
    if not is_admin_group and not is_tenant_group:
        return
    import tenants  # отложенный импорт, см. докстринг модуля relay_tg_to_max
    await tenants.send_group_instructions(chat_id)


@state.dp.message(lambda m: m.text and m.text.startswith("/groups"))
async def cmd_groups(message: TgMessage):
    if message.from_user.id != ADMIN_ID:
        return
    lines = [f"<code>{gid}</code>" for gid in sorted(state.allowed_groups_runtime)]
    await message.reply(
        "<b>Разрешённые группы:</b>\n" + ("\n".join(lines) if lines else "пусто"),
        parse_mode="HTML"
    )


@state.dp.message(lambda m: m.text and m.text.startswith("/link"))
async def cmd_link(message: TgMessage):
    if message.from_user.id != ADMIN_ID:
        # /link — только для тебя (по просьбе); /ping вместо этого теперь
        # общая команда, своя версия — tenants.py
        return

    parts = message.text.strip().split()
    # /link
    # /link 123456789 @nick_tg
    # /link 123456789 -
    if len(parts) == 1:
        rows = await list_mention_links()
        if not rows:
            await message.reply("Привязок нет.\n/link <max_id> @username")
            return
        lines = []
        for row in rows:
            mid, u = row[0], row[1] or ""
            tid = row[2] if len(row) > 2 else None
            if u == "ignore":
                lines.append(f"<code>{mid}</code> → ignore")
            elif tid:
                lines.append(f"<code>{mid}</code> → @{safe_html(u)} (id={tid})")
            else:
                lines.append(f"<code>{mid}</code> → @{safe_html(u)} (нет id)")
        await message.reply("<b>MAX → TG mentions:</b>\n" + "\n".join(lines), parse_mode="HTML")
        return

    if len(parts) != 3:
        await message.reply(
            "Формат:\n"
            "/link 123456789 @nick_tg\n"
            "/link 123456789 -\n"
            "/link — список"
        )
        return

    try:
        max_user_id = int(parts[1])
    except ValueError:
        await message.reply("max_id должен быть числом")
        return

    value = parts[2]
    if value == "-":
        await delete_mention_link(max_user_id)
        await message.reply(f"🗑 Сброшено: <code>{max_user_id}</code> (снова будут уведомления)", parse_mode="HTML")
        return

    if value.lower() == "ignore":
        await set_mention_link(max_user_id, "ignore", None)
        await message.reply(
            f"⏭ <code>{max_user_id}</code> игнорируется (без @ и без уведомлений)",
            parse_mode="HTML",
        )
        return
    raw = value.strip().lstrip("@")
    tg_id = None
    uname = raw
    if raw.isdigit():
        tg_id = int(raw)
        uname = ""
    else:
        try:
            chat = await state.tg_bot.get_chat(f"@{raw}")
            tg_id = int(chat.id)
        except Exception as e:
            state.log.warning(f"get_chat @{raw}: {e}")
            try:
                await state.tg_bot.send_message(
                    ADMIN_ID,
                    (
                        "⚠️ /link: не удалось узнать TG id\n"
                        f"MAX id: <code>{max_user_id}</code>\n"
                        f"username: @{safe_html(raw)}\n"
                        f"ошибка: <code>{safe_html(str(e))}</code>\n\n"
                        f"Можно так:\n<code>/link {max_user_id} 123456789</code>"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    await set_mention_link(max_user_id, uname, tg_id)
    if tg_id:
        await message.reply(
            f"✅ <code>{max_user_id}</code> → @{safe_html(uname)} / <code>{tg_id}</code>",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"⚠️ <code>{max_user_id}</code> → @{safe_html(uname)}\n"
            "id не найден, пока будет обычный @username",
            parse_mode="HTML",
        )


@state.dp.message(lambda m: m.text and m.text.startswith("/addgroup"))
async def cmd_addgroup(message: TgMessage):
    if message.from_user.id != ADMIN_ID:
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Команду нужно писать в группе")
        return

    chat_id = message.chat.id
    title = message.chat.title or ""

    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO allowed_groups (chat_id, title, is_flat) VALUES (?, ?, 0)",
            (chat_id, title)
        )
        await db.commit()

    state.allowed_groups_runtime.add(chat_id)
    await message.reply(
        f"✅ Группа добавлена:\n<code>{chat_id}</code> — {safe_html(title)}",
        parse_mode="HTML"
    )


@state.dp.message(lambda m: m.text and m.text.startswith("/delgroup"))
async def cmd_delgroup(message: TgMessage):
    if message.from_user.id != ADMIN_ID:
        return

    chat_id = message.chat.id
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute("DELETE FROM allowed_groups WHERE chat_id = ?", (chat_id,))
        await db.commit()

    state.allowed_groups_runtime.discard(chat_id)
    await message.reply(
        f"✅ Группа <code>{chat_id}</code> удалена из списка",
        parse_mode="HTML"
    )


@state.dp.message(lambda m: m.text and m.text.startswith("/ping"))
async def cmd_ping(message: TgMessage):
    if message.from_user.id != ADMIN_ID:
        # не глушим — своя версия /ping для тенантов лежит в tenants.py
        raise SkipHandler()

    parts = message.text.strip().split()
    if len(parts) == 1:
        rows = await list_pings()
        if not rows:
            await message.reply("Словарь пуст.\nФормат: /ping слово @username")
            return
        lines = []
        for row in rows:
            k, u = row[0], row[1]
            tid = row[2] if len(row) > 2 else None
            mark = f"id={tid}" if tid else "нет id"
            lines.append(f"<code>{safe_html(k)}</code> → @{safe_html(u)} ({mark})")
        await message.reply("<b>Ping-словарь:</b>\n" + "\n".join(lines), parse_mode="HTML")
        return

    if len(parts) != 3:
        await message.reply(
            "Формат:\n"
            "/ping велиев @nik_tg — добавить\n"
            "/ping велиев - — удалить\n"
            "/ping — список"
        )
        return

    keyword = parts[1]
    value = parts[2]

    if value == "-":
        await delete_ping(keyword)
        await message.reply(f"🗑 Удалено: <code>{safe_html(keyword)}</code>", parse_mode="HTML")
        return

    uname = value.strip().lstrip("@")
    tg_id = None

    if uname.isdigit():
        tg_id = int(uname)
        uname = ""
    else:
        try:
            chat = await state.tg_bot.get_chat(f"@{uname}")
            tg_id = int(chat.id)
        except Exception as e:
            state.log.warning(f"get_chat @{uname}: {e}")
            try:
                await state.tg_bot.send_message(
                    ADMIN_ID,
                    (
                        "⚠️ Не удалось узнать TG id\n"
                        f"слово: <code>{safe_html(keyword)}</code>\n"
                        f"username: @{safe_html(uname)}</code>\n"
                        f"ошибка: <code>{safe_html(str(e))}</code>\n\n"
                        "Перешли боту в личку сообщение этого человека "
                        "и ответь на моё предупреждение:\n"
                        f"<code>/ping {safe_html(keyword)} ID</code>"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    await set_ping(keyword, uname, tg_id)
    if tg_id:
        await message.reply(
            f"✅ <code>{safe_html(keyword.lower())}</code> → "
            f"@{safe_html(uname)} / <code>{tg_id}</code>\n"
            "В чате будет синее исходное слово + уведомление",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"⚠️ <code>{safe_html(keyword.lower())}</code> → @{safe_html(uname)}\n"
            "id не найден, пока будет обычный @username",
            parse_mode="HTML",
        )


@state.dp.message(lambda m: m.text and m.text.startswith("/alias"))
async def cmd_alias(message: TgMessage):
    if message.from_user.id != ADMIN_ID:
        # не глушим — своя версия /alias для тенантов в их группе лежит в tenants.py
        raise SkipHandler()

    # /alias <id> <фамилия|0|->
    # /alias <имя> <id> <фамилия|0|->
    parts = message.text.strip().split()
    if len(parts) == 3:
        # /alias 987654321 Склад  ИЛИ  /alias 987654321 0
        try:
            max_user_id = int(parts[1])
            last = parts[2]
        except ValueError:
            await message.reply(
                "Формат:\n"
                "/alias <id> <фамилия>\n"
                "/alias <id> 0\n"
                "/alias <id> -\n"
                "Пример:\n"
                "/alias 987654321 Склад"
            )
            return
    elif len(parts) == 4:
        # /alias Александр 987654321 Склад
        try:
            max_user_id = int(parts[2])
            last = parts[3]
        except ValueError:
            await message.reply("id должен быть числом")
            return
    else:
        # Если команду написали прямо в теме конкретного личного диалога —
        # в примере подставляем реальный MAX user id собеседника (не
        # max_chat_id — это разные числа, см. докстринг колонки
        # max_sender_id в db_messages.init_db).
        example_id = 987654321
        try:
            dialog = await get_dialog_by_thread(message.chat.id, message.message_thread_id)
            if dialog and dialog["max_sender_id"]:
                example_id = dialog["max_sender_id"]
        except Exception:
            pass

        lines = [
            "Формат:",
            f"<code>/alias {example_id} Склад</code> — фамилия",
            f"<code>/alias {example_id} 0</code> — только имя, без id",
            f"<code>/alias {example_id} -</code> — сбросить (снова будет id)",
            "или:",
            f"<code>/alias Александр {example_id} Склад</code>",
        ]

        existing = await list_aliases()
        if existing:
            lines.append("\n<b>Уже заданы:</b>")
            for mid, last in existing:
                lines.append(f"<code>{mid}</code> → {safe_html(last)}")

        await message.reply("\n".join(lines), parse_mode="HTML")
        return

    if last == "-":
        await delete_alias(max_user_id)
        import relay_tg_to_max  # отложенный импорт, см. докстринг модуля
        await relay_tg_to_max.rename_topic_for_alias(max_user_id, None, db_path=state.DB_PATH)
        await message.reply(f"🗑 Алиас для <code>{max_user_id}</code> сброшен", parse_mode="HTML")
        return

    if last != "0" and not last.strip():
        await message.reply("Укажи фамилию или 0")
        return

    await set_alias(max_user_id, last.strip())
    import relay_tg_to_max  # отложенный импорт, см. докстринг модуля
    await relay_tg_to_max.rename_topic_for_alias(max_user_id, last.strip(), db_path=state.DB_PATH)
    if last == "0":
        await message.reply(
            f"✅ <code>{max_user_id}</code> → только имя (без id)",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"✅ <code>{max_user_id}</code> → фамилия <b>{safe_html(last)}</b>",
            parse_mode="HTML",
        )


@state.dp.message(lambda m: m.text and m.text.startswith("/chats"))
async def list_max_chats(message: TgMessage):
    if message.from_user.id != ADMIN_ID:
        return

    await message.reply("⏳ Собираю чаты...")

    try:
        max_lines = []
        bridge_lines = []

        # --- Чаты MAX из клиента ---
        chats = list(getattr(state.max_client, "chats", None) or [])
        try:
            if hasattr(state.max_client, "fetch_chats"):
                fetched = await state.max_client.fetch_chats()
                if fetched:
                    chats = list(fetched)
        except Exception as e:
            state.log.warning(f"fetch_chats: {e}")

        seen = set()
        for chat in chats:
            chat_id = getattr(chat, "id", None)
            if chat_id is None or chat_id in seen:
                continue
            seen.add(chat_id)
            title = (
                getattr(chat, "title", None)
                or getattr(chat, "name", None)
                or "Без названия"
            )
            kind = "👥" if chat_id < 0 else "👤"
            max_lines.append(f"{kind} <code>{chat_id}</code> — {safe_html(str(title))}")

        # --- Привязки моста ---
        async with aiosqlite.connect(state.DB_PATH) as db:
            async with db.execute(
                "SELECT max_chat_id, telegram_chat_id, telegram_thread_id FROM dialogs ORDER BY max_chat_id"
            ) as cursor:
                rows = await cursor.fetchall()

        for max_id, tg_chat, thread in rows:
            name = "—"
            # Пробуем имя из кэша MAX
            for chat in chats:
                if getattr(chat, "id", None) == max_id:
                    name = getattr(chat, "title", None) or getattr(chat, "name", None) or "—"
                    break
            # Для личных — пробуем get_user
            if name == "—" and max_id > 0:
                try:
                    user = await state.max_client.get_user(max_id)
                    if user and user.names:
                        n = user.names[0]
                        name = f"{n.first_name or ''} {n.last_name or ''}".strip() or n.name or str(max_id)
                except Exception:
                    name = str(max_id)

            if tg_chat == TG_GROUP_ID:
                tg_label = "основная"
            elif tg_chat == TG_GROUP_FLAT:
                tg_label = "плоская"
            else:
                tg_label = str(tg_chat)

            kind = "👥" if max_id < 0 else "👤"
            bridge_lines.append(
                f"{kind} <code>{max_id}</code> — {safe_html(str(name))}\n"
                f"    → {tg_label}, топик <code>{thread}</code>"
            )

        parts = []
        parts.append("<b>📥 Чаты MAX</b>")
        parts.append("\n".join(max_lines) if max_lines else "пусто")
        parts.append("")
        parts.append("<b>🔗 Уже привязаны в мосте</b>")
        parts.append("\n".join(bridge_lines) if bridge_lines else "пусто")

        text = "\n".join(parts)
        if len(text) > 4000:
            text = text[:4000] + "\n…"

        await message.reply(text, parse_mode="HTML")

    except Exception as e:
        state.log.exception("Ошибка /chats")
        await message.reply(f"❌ Ошибка: {e}")


@state.dp.message(lambda m: m.text and m.text.startswith("/debug"))
async def debug_topics(message: TgMessage):
    if message.from_user.id != ADMIN_ID:
        return
    print("=" * 40)
    print("chat_id:", message.chat.id)
    print("chat_title:", message.chat.title)
    print("thread_id:", message.message_thread_id)
    print("text:", message.text)


@state.dp.message(lambda m: m.text and m.text.startswith("/reactions"))
async def debug_reactions(message: TgMessage):
    """Диагностика: спросить у MAX напрямую (get_reactions на уже живом
    подключении, без новой сессии) — проверить, дошла ли реакция до
    сервера, в обход push-события reaction_update (push от MAX на это
    событие приходит не всегда)."""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.reply("Формат:\n/reactions <max_chat_id> <max_message_id>")
        return
    try:
        chat_id = int(parts[1])
        msg_id = int(parts[2])
    except ValueError:
        await message.reply("Оба параметра должны быть числами")
        return

    clients = [("admin", state.max_client)] + [
        (f"tenant {oid}", c) for oid, c in state.tenant_clients.items()
    ]
    for label, client in clients:
        try:
            result = await client.get_reactions(chat_id=chat_id, message_ids=[msg_id])
            await message.reply(f"[{label}] {result!r}"[:4000])
        except Exception as e:
            await message.reply(f"[{label}] ошибка: {e}")


_UPDATE_CONFIRM_WINDOW = 60  # секунд
_pending_update_confirm: float | None = None


@state.dp.message(lambda m: m.text and m.text.startswith("/update"))
async def update_pymax(message: TgMessage):
    global _pending_update_confirm

    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.strip().split()
    is_confirm = len(parts) > 1 and parts[1].lower() == "confirm"

    if not is_confirm:
        # Обновление пакета + рестарт сервиса от root — не должно срабатывать
        # от одного случайного/скомпрометированного сообщения, поэтому нужно
        # явное подтверждение вторым сообщением в течение минуты.
        _pending_update_confirm = time.time()
        await message.reply(
            "⚠️ Это обновит maxapi-python из pip и перезапустит сервис.\n"
            "Подтверди: <code>/update confirm</code> — в течение 60 секунд.",
            parse_mode="HTML",
        )
        return

    if _pending_update_confirm is None or time.time() - _pending_update_confirm > _UPDATE_CONFIRM_WINDOW:
        await message.reply("Нет активного запроса на обновление (или прошло больше 60 секунд) — начни с /update.")
        return
    _pending_update_confirm = None

    await message.reply("⏳ Обновляю maxapi-python...")

    try:
        # Обновляем пакет в текущем venv
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "maxapi-python"],
            capture_output=True,
            text=True,
            timeout=120
        )

        output = (result.stdout or "") + (result.stderr or "")
        short_output = output[-1500:] if len(output) > 1500 else output

        if result.returncode == 0:
            await message.reply(
                f"✅ Обновление завершено\n\n"
                f"<pre>{html.escape(short_output)}</pre>\n\n"
                f"Перезапускаю сервис...",
                parse_mode="HTML"
            )

            # Перезапуск через systemd
            subprocess.Popen(
                ["systemctl", "restart", "maxbridge"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            await message.reply(
                f"❌ Ошибка обновления:\n<pre>{html.escape(short_output)}</pre>",
                parse_mode="HTML"
            )

    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@state.dp.message(lambda m: m.text and m.text.startswith("/bind"))
async def bind_topic(message: TgMessage):
    if message.from_user.id != ADMIN_ID:
        return
    if message.chat.id not in state.allowed_groups_runtime:
        return

    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.reply("Формат:\n/bind <max_chat_id>\nПример:\n/bind -10000000000001")
        return

    try:
        max_chat_id = int(parts[1])
    except ValueError:
        await message.reply("max_chat_id должен быть числом")
        return

    # Есть топик → привязка к топику, нет → ко всей группе
    thread_id = message.message_thread_id  # может быть None

    await save_thread(max_chat_id, message.chat.id, thread_id)
    await message.reply(
        f"✅ Привязано к MAX `{max_chat_id}`"
        + (" (вся группа, без тем)" if thread_id is None else f" / топик {thread_id}")
    )
    state.log.info(f"BIND max={max_chat_id} -> {message.chat.id}/{thread_id}")
