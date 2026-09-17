"""
Направление Telegram -> MAX: очередь отправки исходящих в TG (tg_worker,
используется обеими сторонами моста), создание топиков и сама пересылка
сообщений из Telegram в MAX.

telegram_to_max() — catch-all @dp.message() без фильтра, поэтому этот
модуль должен импортироваться в bridge.py ПОСЛЕДНИМ среди всех модулей,
регистрирующих @dp.message(...) — иначе он перехватит апдейты раньше более
специфичных обработчиков (auth_flow.py, commands.py, tenants.py).

_resolve_route() определяет, какому MAX-клиенту и какой БД принадлежит
входящее TG-сообщение: твоей (admin, allowed_groups_runtime/state.DB_PATH,
без изменений) или чьему-то из tenants (state.tenant_group_map/tenant_clients).
"""
import os

from pymax import Client, Message as MaxMessage, Photo, File, Video, Voice, ApiError
from aiogram.types import Message as TgMessage, MessageReactionUpdated, ReactionTypeEmoji
from aiogram.exceptions import TelegramRetryAfter

from config import TG_GROUP_FLAT, FLAT_MAX_CHAT_ID
from db_messages import (
    get_max_message_id, save_message_mapping, get_thread, save_thread,
    find_topic_name_collision, get_thread_by_sender_id,
)
from db_meta import get_alias
from media import download_tg_file, safe_remove, process_media_group
import state

import aiosqlite
import asyncio


async def _get_verified_user(client: Client, user_id: int):
    """client.get_user() у pymax иногда возвращает профиль не того id, что
    запросили (похоже на гонку сопоставления запрос/ответ при холодном
    кэше — кэш пустой сразу после рестарта бота). Перепроверяем id в ответе
    и один раз перезапрашиваем, прежде чем сдаться."""
    for attempt in range(2):
        user = await client.get_user(user_id)
        if user is None or user.id == user_id:
            return user
        state.log.warning(f"get_user({user_id}) вернул чужой профиль id={user.id}, попытка {attempt + 1}")
    return None


def _is_missing_thread_error(e: Exception) -> bool:
    """Telegram отвечает на попытку писать в удалённую тему примерно так:
    'Bad Request: message thread not found' (иногда без 'Bad Request:')."""
    text = str(e).lower()
    return "thread not found" in text or "topic_deleted" in text


def _is_content_send_error(e: Exception) -> bool:
    """Ошибка из-за конкретного сообщения/файла (битая ссылка, слишком
    большой файл и т.п.) — не значит, что мост/группа сломаны, в отличие от
    прав доступа или удалённой группы. Не должна поднимать общую тревогу
    с чек-листом "бот всё ещё админ?" — при сбое пересылки одного файла
    такой чек-лист не в тему и только пугает."""
    text = str(e).lower()
    return any(s in text for s in (
        "failed to get http url content",
        "wrong file identifier",
        "wrong type of the web page content",
        "file is too big",
    ))


def _is_size_related_error(e: Exception) -> bool:
    """Только "file is too big" — прямой и однозначный отказ Telegram по
    размеру. Остальные варианты из _is_content_send_error (битая ссылка,
    неправильный тип содержимого) тоже мешают отправить видео целиком, но
    могут быть вообще не про размер — не стоит утверждать это в тексте
    для пользователя, если точно не знаем."""
    return "file is too big" in str(e).lower()


async def _recreate_topic(
    tg_group_id: int, max_chat_id: int, db_path: str, max_sender_id: int | None = None
) -> int | None:
    """Старая тема для этого MAX-диалога удалена — стираем протухшую
    привязку и заводим новую тему взамен, с тем же именем, что взял бы
    get_or_create_topic (название группы MAX, или имя собеседника для
    личных диалогов — max_sender_id тут тот же id, что уже лежал в задаче
    очереди, ровно то же самое поле, которым пользуется get_or_create_topic)."""
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("DELETE FROM dialogs WHERE max_chat_id = ?", (max_chat_id,))
            await db.commit()

        name = f"MAX чат {max_chat_id}"
        no_last_name = False
        name_collision = False
        client, _ = _resolve_route(tg_group_id)
        if client is not None:
            try:
                if max_chat_id < 0:
                    chat = await client.get_chat(max_chat_id)
                    if chat and chat.title:
                        name = f"👥 {chat.title}"
                elif max_sender_id:
                    user = await _get_verified_user(client, max_sender_id)
                    if user and user.names:
                        n = user.names[0]
                        first = (n.first_name or n.name or "").strip()
                        if n.first_name and n.last_name:
                            name = f"{n.first_name} {n.last_name}"
                        elif n.first_name:
                            name = n.first_name
                            no_last_name = True
                        else:
                            name = n.name or name
                            no_last_name = True

                        if not no_last_name:
                            name_collision = await find_topic_name_collision(
                                tg_group_id, name, exclude_max_chat_id=max_chat_id, db_path=db_path
                            )

                        # Ключ алиаса — реальный MAX user id (max_sender_id),
                        # НЕ max_chat_id (см. докстринг колонки в db_messages.init_db).
                        alias = await get_alias(max_sender_id, db_path=db_path)
                        if alias is not None and first:
                            name = first if alias == "0" else f"{first} {alias}"
                            no_last_name = False
                            name_collision = False
            except Exception as e:
                state.log.warning(f"Не удалось получить имя для max_chat_id={max_chat_id}: {e}")

        topic = await state.tg_bot.create_forum_topic(
            chat_id=tg_group_id,
            name=name[:128],
        )
        thread_id = topic.message_thread_id
        await save_thread(
            max_chat_id, tg_group_id, thread_id,
            topic_name=name[:128], max_sender_id=max_sender_id, db_path=db_path,
        )
        state.log.info(f"Тема для max_chat_id={max_chat_id} была удалена, создал новую: {thread_id}")

        note = "ℹ️ Прошлая тема для этого диалога была удалена — создал новую, переписка продолжится здесь."
        if max_chat_id > 0 and (no_last_name or name_collision):
            if no_last_name:
                note += (
                    "\n\nУ этого пользователя MAX не указана фамилия в профиле — чтобы в будущем не "
                    "путать его с другими пользователями с таким же именем, рекомендую задать метку:\n"
                )
            else:
                note += (
                    "\n\nУ тебя уже есть другой контакт с таким же именем и фамилией — их легко перепутать. "
                    "Чтобы отличать их, задай метку в этой теме:\n"
                )
            note += f"<code>/alias {max_sender_id} Красавчик</code>"
        try:
            await state.tg_bot.send_message(
                tg_group_id,
                note,
                message_thread_id=thread_id,
                parse_mode="HTML" if (no_last_name or name_collision) else None,
            )
        except Exception:
            pass
        return thread_id
    except Exception as e:
        state.log.error(f"Не удалось пересоздать тему для max_chat_id={max_chat_id}: {e}")
        return None


async def rename_topic_for_alias(max_user_id: int, alias: str | None, db_path: str):
    """Вызывается после /alias — max_user_id тут настоящий MAX user id
    собеседника (НЕ max_chat_id — см. докстринг колонки max_sender_id в
    db_messages.init_db). Если у этого id уже есть своя личная тема (не
    групповая — там алиас влияет только на подпись отправителя в тексте
    сообщения, не на название темы), переименовывает её."""
    mapping = await get_thread_by_sender_id(max_user_id, db_path=db_path)
    if not mapping or not mapping["telegram_thread_id"]:
        return

    client, _ = _resolve_route(mapping["telegram_chat_id"])
    if client is None:
        return

    first = "Unknown"
    max_last = ""
    try:
        user = await _get_verified_user(client, max_user_id)
        if user is None:
            state.log.warning(f"Не удалось получить надёжный профиль {max_user_id} — переименование отменено")
            return
        if user.names:
            n = user.names[0]
            first = (n.first_name or n.name or first).strip()
            max_last = (n.last_name or "").strip()
    except Exception as e:
        state.log.warning(f"Не удалось получить имя для переименования темы {max_user_id}: {e}")
        return

    if alias is None:
        # алиас сбросили — откатываемся ровно к тому, что взял бы
        # get_or_create_topic при создании темы с нуля (см. format_sender_name)
        new_name = f"{first} {max_last}".strip() if max_last else f"{first} {max_user_id}"
    elif alias == "0":
        new_name = first
    else:
        new_name = f"{first} {alias}"
    new_name = new_name[:128]

    try:
        await state.tg_bot.edit_forum_topic(
            chat_id=mapping["telegram_chat_id"],
            message_thread_id=mapping["telegram_thread_id"],
            name=new_name,
        )
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE dialogs SET topic_name = ? WHERE max_chat_id = ?",
                (new_name, mapping["max_chat_id"]),
            )
            await db.commit()
    except Exception as e:
        state.log.warning(f"Не удалось переименовать тему при алиасе {max_user_id}: {e}")


def _resolve_route(chat_id: int):
    """(max_client, db_path) для TG-чата — твой мост или чей-то из tenants.
    (None, None), если чат никому не принадлежит."""
    if chat_id in state.allowed_groups_runtime:
        return state.max_client, state.DB_PATH
    owner_id = state.tenant_group_map.get(chat_id)
    if owner_id is not None:
        client = state.tenant_clients.get(owner_id)
        if client is not None:
            return client, state.tenant_db_path(owner_id)
    return None, None


_TG_SEND_METHODS = {
    "send_message", "send_photo", "send_voice", "send_video", "send_document", "send_media_group",
}


async def _send_tg_method(method: str, kwargs: dict):
    """Обёртка над отправкой в Telegram, которая сама пересыпает при flood
    control (TelegramRetryAfter) — иначе всплеск сообщений (например добор
    пропущенного разом после реконнекта) рискует потратить весь лимит
    обычных ретраев tg_worker'а только на ожидание лимита Telegram, не
    оставив попыток на настоящие ошибки. Не трогает остальную логику
    tg_worker (пересоздание темы, финальные уведомления) — просто
    гарантирует, что сама отправка в итоге пройдёт, если Telegram не
    отказал по существу."""
    last_exc = None
    for _ in range(6):
        try:
            if method == "send_message":
                return await state.tg_bot.send_message(**kwargs)
            elif method == "send_photo":
                return await state.tg_bot.send_photo(**kwargs)
            elif method == "send_voice":
                return await state.tg_bot.send_voice(**kwargs)
            elif method == "send_video":
                return await state.tg_bot.send_video(**kwargs)
            elif method == "send_document":
                return await state.tg_bot.send_document(**kwargs)
            elif method == "send_media_group":
                result = await state.tg_bot.send_media_group(**kwargs)
                return result[0] if result and isinstance(result, list) and result else None
        except TelegramRetryAfter as e:
            last_exc = e
            wait = e.retry_after + 0.5
            state.log.warning(f"Telegram просит подождать {wait:.1f}с (флуд-контроль), метод={method}")
            await asyncio.sleep(wait)
    raise last_exc


async def tg_worker():
    while True:
        task = await state.tg_queue.get()
        method = task.get("method")
        kwargs = task.get("kwargs", {})
        max_chat_id = task.get("max_chat_id")
        max_message_id = task.get("max_message_id")
        tmp_path = task.get("tmp_path")  # ← добавили
        body_text = task.get("body_text")
        max_sender_id = task.get("max_sender_id")
        db_path = task.get("db_path") or state.DB_PATH
        retries = 2

        sent_message = None
        recreated_topic = False

        for attempt in range(retries + 1):
            try:
                if method not in _TG_SEND_METHODS:
                    state.log.warning(f"Неизвестный метод в очереди: {method}")
                    break
                sent_message = await _send_tg_method(method, kwargs)

                # Telegram не всегда кидает ошибку на удалённую тему — иногда
                # молча доставляет в General вместо неё. Ловим это по
                # несовпадению thread_id, раз явного исключения не было.
                intended_thread = kwargs.get("message_thread_id")
                actual_thread = getattr(sent_message, "message_thread_id", None) if sent_message else None
                if (
                    sent_message is not None
                    and not recreated_topic
                    and intended_thread is not None
                    and actual_thread != intended_thread
                    and max_chat_id is not None
                    and kwargs.get("chat_id") is not None
                ):
                    recreated_topic = True
                    stray_message = sent_message
                    new_thread_id = await _recreate_topic(kwargs["chat_id"], max_chat_id, db_path, max_sender_id)
                    if new_thread_id is not None:
                        kwargs["message_thread_id"] = new_thread_id
                        sent_message = None
                        try:
                            await state.tg_bot.delete_message(kwargs["chat_id"], stray_message.message_id)
                        except Exception:
                            pass
                        continue

                break
            except Exception as e:
                # Тема удалена — пересоздаём её и пробуем ещё раз, не дожидаясь
                # исчерпания обычных ретраев (повтор в ту же удалённую тему
                # всё равно не поможет).
                if (
                    not recreated_topic
                    and max_chat_id is not None
                    and kwargs.get("message_thread_id") is not None
                    and kwargs.get("chat_id") is not None
                    and _is_missing_thread_error(e)
                ):
                    recreated_topic = True
                    new_thread_id = await _recreate_topic(kwargs["chat_id"], max_chat_id, db_path, max_sender_id)
                    if new_thread_id is not None:
                        kwargs["message_thread_id"] = new_thread_id
                        continue

                if attempt == retries:
                    state.log.error(f"Ошибка отправки из очереди после {retries + 1} попыток: {e}")
                    chat_id = kwargs.get("chat_id")

                    # Видео без подтверждённого размера, которое всё же не
                    # получилось отправить целиком (например реально
                    # оказалось большим) — откатываемся на превью вместо
                    # того чтобы просто показать ошибку (см. relay_max_to_tg
                    # .py, media_items[...]["fallback_thumbnail"]).
                    fallback = task.get("fallback")
                    if fallback and _is_content_send_error(e):
                        # Причину дописываем именно тут, а не заранее в
                        # relay_max_to_tg.py — там ещё нет настоящей ошибки
                        # отправки, только предположение "размер не
                        # подтверждён". "file is too big" — однозначно про
                        # размер, остальное (битая ссылка, неправильный тип
                        # содержимого) может быть вообще не про размер, не
                        # стоит утверждать это пользователю без уверенности.
                        reason = (
                            "🎬 Видео больше 50 МБ — только превью"
                            if _is_size_related_error(e)
                            else "🎬 Видео — не удалось отправить целиком, только превью"
                        )
                        fallback["kwargs"]["caption"] = fallback["kwargs"].get("caption", "") + f"<i>{reason}</i>"
                        try:
                            sent_message = await _send_tg_method(fallback["method"], fallback["kwargs"])
                            state.log.info("Видео целиком не отправилось, использован fallback на превью")
                        except Exception as fe:
                            state.log.warning(f"Fallback на превью тоже не удался: {fe}")

                    if not sent_message:
                        if _is_content_send_error(e):
                            # Проблема в конкретном файле/ссылке, не в группе —
                            # без общей тревоги с чек-листом про админку, только
                            # тихая заметка в саму тему, если она известна.
                            if chat_id is not None and kwargs.get("message_thread_id") is not None:
                                try:
                                    await state.tg_bot.send_message(
                                        chat_id,
                                        "⚠️ Не удалось переслать это сообщение из MAX (проблема с файлом или ссылкой) — "
                                        "остальное должно приходить как обычно.",
                                        message_thread_id=kwargs["message_thread_id"],
                                    )
                                except Exception:
                                    pass
                        else:
                            owner_id = state.tenant_group_map.get(chat_id) if chat_id is not None else None
                            if owner_id is not None:
                                import tenants  # отложенный импорт, см. докстринг модуля
                                await tenants.notify_group_broken(owner_id, chat_id, f"не смог отправить сообщение ({e})")
                else:
                    await asyncio.sleep(1.5 * (attempt + 1))

        # Сохраняем маппинг
        if sent_message and max_chat_id and max_message_id:
            try:
                await save_message_mapping(
                    max_chat_id=max_chat_id,
                    max_message_id=max_message_id,
                    tg_chat_id=sent_message.chat.id,
                    tg_thread_id=getattr(sent_message, "message_thread_id", None),
                    tg_message_id=sent_message.message_id,
                    body_text=body_text,
                    max_sender_id=max_sender_id,
                    db_path=db_path,
                )

            except Exception as e:
                state.log.warning(f"Не удалось сохранить маппинг сообщения: {e}")
        followup = task.get("followup_text")
        if followup and sent_message:
            try:
                rk = {
                    "chat_id": kwargs["chat_id"],
                    "text": followup,
                    "parse_mode": "HTML",
                    "reply_to_message_id": sent_message.message_id,
                }
                if kwargs.get("message_thread_id") is not None:
                    rk["message_thread_id"] = kwargs["message_thread_id"]
                await state.tg_bot.send_message(**rk)
            except Exception as e:
                state.log.warning(f"followup после медиа: {e}")

        # Удаляем временный файл, если он был
        if tmp_path:
            await safe_remove(tmp_path)

        state.tg_queue.task_done()


async def get_or_create_topic(message: MaxMessage, client: Client, *, tg_group_id: int, db_path: str) -> int | None:
    max_chat_id = message.chat_id

    # Лок с db_path в ключе — если один MAX-чат шарится между несколькими
    # твоими аккаунтами (общая группа), у каждого своя независимая
    # сериализация создания темы в СВОЕЙ TG-группе, а не общая на всех
    # (та же природа, что и баг с дедупликацией сообщений).
    async with state.topic_locks[(db_path, max_chat_id)]:
        mapping = await get_thread(max_chat_id, db_path=db_path, default_tg_group_id=tg_group_id)
        if mapping:
            return mapping["telegram_thread_id"]

        # Название темы
        no_last_name = False
        name_collision = False
        if max_chat_id < 0:
            name = "👥 MAX группа"
            try:
                chat = await client.get_chat(max_chat_id)
                if chat and chat.title:
                    name = f"👥 {chat.title}"
            except Exception as e:
                state.log.warning(f"Не удалось получить название группы {max_chat_id}: {e}")
        else:
            name = "Unknown"
            first = "Unknown"
            try:
                user = await _get_verified_user(client, message.sender)
                if user and user.names:
                    n = user.names[0]
                    first = (n.first_name or n.name or first).strip()
                    if n.first_name and n.last_name:
                        name = f"{n.first_name} {n.last_name}"
                    elif n.first_name:
                        name = n.first_name
                        no_last_name = True
                    else:
                        name = n.name or str(message.sender)
                        no_last_name = True
            except Exception as e:
                state.log.warning(f"Не удалось получить имя пользователя {message.sender}: {e}")

            # Совпадение полного ФИО с уже существующей темой — редкий случай,
            # проверяем только когда фамилия вообще есть (иначе сработает
            # более частая проверка на её отсутствие, см. no_last_name выше).
            name_collision = False
            if not no_last_name:
                name_collision = await find_topic_name_collision(
                    tg_group_id, name, exclude_max_chat_id=max_chat_id, db_path=db_path
                )

            # Алиас всегда в приоритете — та же логика, что и в format_sender_name.
            # Ключ — реальный MAX user id (message.sender), НЕ max_chat_id
            # (см. докстринг колонки max_sender_id в db_messages.init_db).
            alias = await get_alias(message.sender, db_path=db_path)
            if alias is not None:
                name = first if alias == "0" else f"{first} {alias}"
                no_last_name = False
                name_collision = False

        try:
            state.log.info(f"Создаю тему: {name} (max_chat_id={max_chat_id})")
            topic = await state.tg_bot.create_forum_topic(
                chat_id=tg_group_id,
                name=name[:128]  # ограничение Telegram
            )
            thread_id = topic.message_thread_id
            await save_thread(
                max_chat_id, tg_group_id, thread_id, topic_name=name[:128],
                max_sender_id=message.sender if max_chat_id > 0 else None, db_path=db_path,
            )

            if max_chat_id > 0 and (no_last_name or name_collision):
                if no_last_name:
                    hint = (
                        "ℹ️ У этого пользователя MAX не указана фамилия в профиле — чтобы в будущем не "
                        "путать его с другими пользователями с таким же именем, рекомендую задать метку:\n"
                    )
                else:
                    hint = (
                        "ℹ️ У тебя уже есть другой контакт с таким же именем и фамилией — их легко перепутать.\n\n"
                        "Чтобы отличать их, задай метку в этой теме:\n"
                    )
                try:
                    await state.tg_bot.send_message(
                        tg_group_id,
                        hint + f"<code>/alias {message.sender} Красавчик</code>",
                        message_thread_id=thread_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            return thread_id
        except Exception as e:
            state.log.error(f"Ошибка создания темы: {e}")
            owner_id = state.tenant_group_map.get(tg_group_id)
            if owner_id is not None:
                import tenants  # отложенный импорт, см. докстринг модуля
                await tenants.notify_group_broken(owner_id, tg_group_id, f"не смог создать тему ({e})")
            return None


def _get_forward_origin_name(message: TgMessage) -> str | None:
    """Имя, ОТ КОГО переслано сообщение — та же двойная совместимость
    (forward_from/forward_origin), что уже используется в auth_flow.py's
    admin_forward_id. Зеркалит fwd_line, который MAX → TG уже показывает —
    раньше эта пометка была только в одну сторону."""
    src = message.forward_from
    if src is not None:
        return src.full_name or src.first_name or (f"@{src.username}" if src.username else None)

    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None

    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        return (
            sender_user.full_name
            or sender_user.first_name
            or (f"@{sender_user.username}" if sender_user.username else None)
        )

    sender_user_name = getattr(origin, "sender_user_name", None)  # скрыл профиль, но разрешил имя
    if sender_user_name:
        return sender_user_name

    chat = getattr(origin, "chat", None)  # переслано из канала/группы
    if chat is not None and getattr(chat, "title", None):
        return chat.title

    author_signature = getattr(origin, "author_signature", None)
    if author_signature:
        return author_signature

    return None


async def _report_tg_to_max_failure(message: TgMessage, what: str, e: Exception):
    """Раньше ошибка скачивания/отправки файла из TG в MAX просто улетала
    наверх необработанной — пользователь не видел вообще ничего ни в TG,
    ни в MAX, ошибка оставалась только в логах. Теперь хотя бы короткая
    заметка в саму тему."""
    text = str(e)
    if "file is too big" in text.lower():
        note = "Telegram не разрешил боту скачать файл (лимит на скачивание — 20 МБ)."
    else:
        note = text
    state.log.error(f"Не удалось переслать {what} TG → MAX: {e}")
    try:
        await state.tg_bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=message.message_thread_id,
            text=f"⚠️ Не удалось переслать {what} в MAX: {note}",
        )
    except Exception:
        pass


@state.dp.message()
async def telegram_to_max(message: TgMessage):
    max_client, db_path = _resolve_route(message.chat.id)
    if max_client is None:
        return

    owner_id = state.tenant_group_map.get(message.chat.id)
    if owner_id is not None and not getattr(message.chat, "is_forum", True):
        import tenants  # отложенный импорт, см. докстринг модуля
        await tenants.notify_group_broken(owner_id, message.chat.id, "в группе отключили «Темы»")
        return

    if message.text and message.text.startswith((
        "/bind", "/debug", "/update", "/chats", "/unbind",
        "/groups", "/addgroup", "/delgroup", "/alias", "/ping", "/link",
        "/setupgroup", "/retry",
    )):
        return

    # === Обработка альбомов ===
    if message.media_group_id:
        media_group_id = message.media_group_id

        if media_group_id not in state.media_group_buffer:
            state.media_group_buffer[media_group_id] = []

        state.media_group_buffer[media_group_id].append(message)

        # Отменяем предыдущий таймер, если был
        old_timer = state.media_group_timers.get(media_group_id)
        if old_timer and not old_timer.done():
            old_timer.cancel()

        # Ставим новый таймер
        async def delayed_process():
            await asyncio.sleep(state.MEDIA_GROUP_DELAY)
            await process_media_group(media_group_id)

        state.media_group_timers[media_group_id] = asyncio.create_task(delayed_process())
        return
    try:
        if message.chat.id == TG_GROUP_FLAT:
            max_chat_id = FLAT_MAX_CHAT_ID
        else:
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    """
                    SELECT max_chat_id FROM dialogs
                    WHERE telegram_chat_id = ? AND telegram_thread_id = ?
                    """,
                    (message.chat.id, message.message_thread_id)
                ) as cursor:
                    row = await cursor.fetchone()

            if not row:
                state.log.warning(f"Нет связи для thread_id={message.message_thread_id}")
                return
            max_chat_id = row[0]

        text = message.caption or message.text or ""
        forward_name = _get_forward_origin_name(message)
        if forward_name:
            # **...** — MAX сам разбирает Markdown при отправке (Formatter.format_markdown),
            # так что это реально станет жирным, как и fwd_line в сторону MAX → TG.
            marker = f"🔁 **Переслано от {forward_name}**"
            text = f"{marker}\n{text}" if text else marker

        # Определяем reply_to
        reply_to_max_id = None
        if message.reply_to_message:
            mapping = await get_max_message_id(
                message.chat.id,
                message.reply_to_message.message_id,
                db_path=db_path,
            )
            if mapping:
                reply_to_max_id = mapping[1]  # max_message_id

        # --- Проверка размера ---
        file_size = 0
        if message.photo:
            file_size = message.photo[-1].file_size or 0
        elif message.voice:
            file_size = message.voice.file_size or 0
        elif message.audio:
            file_size = message.audio.file_size or 0
        elif message.video:
            file_size = message.video.file_size or 0
        elif message.video_note:
            file_size = message.video_note.file_size or 0
        elif message.sticker:
            file_size = message.sticker.file_size or 0
        elif message.document:
            file_size = message.document.file_size or 0

        if file_size > state.TG_DOWNLOAD_LIMIT:
            await state.tg_bot.send_message(
                chat_id=message.chat.id,
                message_thread_id=message.message_thread_id,
                text="📎 Файл больше 20 МБ — столько Telegram разрешает боту скачать, отправить в MAX не получится."
            )
            return

        tmp_path = None

        # ----- ФОТО -----
        if message.photo:
            try:
                photo = message.photo[-1]
                tmp_path = await download_tg_file(photo.file_id, suffix=".jpg")
                sent = await max_client.send_message(
                    chat_id=max_chat_id,
                    text=text or "",
                    reply_to=reply_to_max_id,
                    attachments=[Photo(path=tmp_path)]
                )
                if sent and getattr(sent, "id", None):
                    await save_message_mapping(
                        max_chat_id=max_chat_id,
                        max_message_id=sent.id,
                        tg_chat_id=message.chat.id,
                        tg_thread_id=message.message_thread_id,
                        tg_message_id=message.message_id,
                        db_path=db_path,
                    )
                state.log.info("TG → MAX: фото")
            except Exception as e:
                await _report_tg_to_max_failure(message, "фото", e)
            finally:
                await safe_remove(tmp_path)
            return

        # ----- ГОЛОСОВОЕ -----
        if message.voice:
            try:
                tmp_path = await download_tg_file(message.voice.file_id, suffix=".ogg")
                # Voice, а не File — иначе MAX показывает голосовое как обычный
                # файл-вложение, а не как голосовое сообщение. Но: у pymax 2.4.1
                # есть встроенный авто-повтор именно для ошибки
                # "attachment.not.ready", а MAX теперь стабильно отвечает более
                # новым кодом "errors.process.attachment.video.not.ready" —
                # библиотека его не узнаёт (точное сравнение строк) и никогда
                # не ждёт готовности сама. Правильный фикс потребовал бы своего
                # цикла загрузка+ожидание+повтор через приватные части pymax —
                # слишком хрупко (сломается на следующем /update). Вместо
                # этого: одна попытка как Voice, и если именно эта ошибка —
                # откат на File (то, что и так стабильно работало раньше, просто
                # выглядит как файл, а не как голосовое сообщение).
                try:
                    sent = await max_client.send_message(
                        chat_id=max_chat_id,
                        text=text,
                        reply_to=reply_to_max_id,
                        attachments=[Voice(path=tmp_path, duration=(message.voice.duration or 0) * 1000)]
                    )
                except ApiError as e:
                    if "not.ready" not in str(e.error or e).lower():
                        raise
                    state.log.warning(f"Voice не готов на стороне MAX, отправляю как File: {e}")
                    sent = await max_client.send_message(
                        chat_id=max_chat_id,
                        text=text,
                        reply_to=reply_to_max_id,
                        attachments=[File(path=tmp_path, name="voice.ogg")]
                    )
                if sent and getattr(sent, "id", None):
                    await save_message_mapping(
                        max_chat_id=max_chat_id,
                        max_message_id=sent.id,
                        tg_chat_id=message.chat.id,
                        tg_thread_id=message.message_thread_id,
                        tg_message_id=message.message_id,
                        db_path=db_path,
                    )
                state.log.info("TG → MAX: голосовое")
            except Exception as e:
                await _report_tg_to_max_failure(message, "голосовое", e)
            finally:
                await safe_remove(tmp_path)
            return

        # ----- АУДИО -----
        if message.audio:
            try:
                ext = os.path.splitext(message.audio.file_name or "")[1] or ".mp3"
                tmp_path = await download_tg_file(message.audio.file_id, suffix=ext)
                # name= явно — иначе MAX берёт имя из временного файла
                # (случайные буквы/цифры), а не настоящее имя из Telegram.
                original_name = message.audio.file_name or (message.audio.title or "audio") + ext
                sent = await max_client.send_message(
                    chat_id=max_chat_id,
                    text=text,
                    reply_to=reply_to_max_id,
                    attachments=[File(path=tmp_path, name=original_name)]
                )
                if sent and getattr(sent, "id", None):
                    await save_message_mapping(
                        max_chat_id=max_chat_id,
                        max_message_id=sent.id,
                        tg_chat_id=message.chat.id,
                        tg_thread_id=message.message_thread_id,
                        tg_message_id=message.message_id,
                        db_path=db_path,
                    )
                state.log.info("TG → MAX: аудио")
            except Exception as e:
                await _report_tg_to_max_failure(message, "аудио", e)
            finally:
                await safe_remove(tmp_path)
            return

        # ----- ВИДЕО -----
        if message.video:
            try:
                tmp_path = await download_tg_file(message.video.file_id, suffix=".mp4")
                sent = await max_client.send_message(
                    chat_id=max_chat_id,
                    text=text,
                    reply_to=reply_to_max_id,
                    attachments=[Video(path=tmp_path)]
                )
                if sent and getattr(sent, "id", None):
                    await save_message_mapping(
                        max_chat_id=max_chat_id,
                        max_message_id=sent.id,
                        tg_chat_id=message.chat.id,
                        tg_thread_id=message.message_thread_id,
                        tg_message_id=message.message_id,
                        db_path=db_path,
                    )
                state.log.info("TG → MAX: видео")
            except Exception as e:
                await _report_tg_to_max_failure(message, "видео", e)
            finally:
                await safe_remove(tmp_path)
            return

        # ----- КРУЖОЧЕК -----
        if message.video_note:
            try:
                tmp_path = await download_tg_file(message.video_note.file_id, suffix=".mp4")
                sent = await max_client.send_message(
                    chat_id=max_chat_id,
                    text=text or "",
                    reply_to=reply_to_max_id,
                    attachments=[Video(path=tmp_path)]
                )
                if sent and getattr(sent, "id", None):
                    await save_message_mapping(
                        max_chat_id=max_chat_id,
                        max_message_id=sent.id,
                        tg_chat_id=message.chat.id,
                        tg_thread_id=message.message_thread_id,
                        tg_message_id=message.message_id,
                        db_path=db_path,
                    )
                state.log.info("TG → MAX: кружочек")
            except Exception as e:
                await _report_tg_to_max_failure(message, "видео-кружок", e)
            finally:
                await safe_remove(tmp_path)
            return

        # ----- СТИКЕР -----
        if message.sticker:
            try:
                if message.sticker.is_animated:
                    ext = ".tgs"
                elif message.sticker.is_video:
                    ext = ".webm"
                else:
                    ext = ".webp"

                tmp_path = await download_tg_file(message.sticker.file_id, suffix=ext)

                emoji = message.sticker.emoji or ""
                sticker_caption = text or f"Стикер {emoji}".strip()

                sent = None
                if ext == ".webp":
                    try:
                        sent = await max_client.send_message(
                            chat_id=max_chat_id,
                            text=sticker_caption,
                            reply_to=reply_to_max_id,
                            attachments=[Photo(path=tmp_path)]
                        )
                        state.log.info("TG → MAX: стикер (как фото)")
                    except Exception as e:
                        state.log.warning(f"Стикер как Photo не отправился: {e}")

                if not sent:
                    sent = await max_client.send_message(
                        chat_id=max_chat_id,
                        text=sticker_caption,
                        reply_to=reply_to_max_id,
                        attachments=[File(path=tmp_path)]
                    )
                    state.log.info("TG → MAX: стикер (как файл)")

                if sent and getattr(sent, "id", None):
                    await save_message_mapping(
                        max_chat_id=max_chat_id,
                        max_message_id=sent.id,
                        tg_chat_id=message.chat.id,
                        tg_thread_id=message.message_thread_id,
                        tg_message_id=message.message_id,
                        db_path=db_path,
                    )
            except Exception as e:
                await _report_tg_to_max_failure(message, "стикер", e)
            finally:
                await safe_remove(tmp_path)
            return

        # ----- ДОКУМЕНТ -----
        if message.document:
            try:
                original_name = message.document.file_name or "file"
                ext = os.path.splitext(original_name)[1] or ""
                tmp_path = await download_tg_file(message.document.file_id, suffix=ext)
                # name= явно — иначе MAX берёт имя из временного файла
                # (случайные буквы/цифры).
                sent = await max_client.send_message(
                    chat_id=max_chat_id,
                    text=text,
                    reply_to=reply_to_max_id,
                    attachments=[File(path=tmp_path, name=original_name)]
                )
                if sent and getattr(sent, "id", None):
                    await save_message_mapping(
                        max_chat_id=max_chat_id,
                        max_message_id=sent.id,
                        tg_chat_id=message.chat.id,
                        tg_thread_id=message.message_thread_id,
                        tg_message_id=message.message_id,
                        db_path=db_path,
                    )
                state.log.info("TG → MAX: документ")
            except Exception as e:
                await _report_tg_to_max_failure(message, "документ", e)
            finally:
                await safe_remove(tmp_path)
            return

        # ----- ПРОСТО ТЕКСТ -----
        if text:
            sent = await max_client.send_message(
                chat_id=max_chat_id,
                text=text,
                reply_to=reply_to_max_id
            )
            if sent and getattr(sent, "id", None):
                await save_message_mapping(
                    max_chat_id=max_chat_id,
                    max_message_id=sent.id,
                    tg_chat_id=message.chat.id,
                    tg_thread_id=message.message_thread_id,
                    tg_message_id=message.message_id,
                    db_path=db_path,
                )
            state.log.info("TG → MAX: текст")
            return

        # ----- Всё остальное -----
        state.log.info(f"TG → MAX: неизвестный тип {message.content_type} (игнорирую)")

    except Exception as e:
        state.log.exception(f"Ошибка Telegram → MAX: {e}")


@state.dp.edited_message()
async def telegram_edited_to_max(message: TgMessage):
    max_client, db_path = _resolve_route(message.chat.id)
    if max_client is None:
        return
    if not message.message_thread_id:
        return

    try:
        mapping = await get_max_message_id(message.chat.id, message.message_id, db_path=db_path)
        if not mapping:
            state.log.warning(f"Нет маппинга для отредактированного сообщения tg_id={message.message_id}")
            return

        max_chat_id, max_message_id = mapping
        new_text = message.text or message.caption or ""

        if not new_text:
            state.log.info("Редактирование без текста — пропускаю")
            return

        await max_client.edit_message(
            chat_id=max_chat_id,
            message_id=max_message_id,
            text=new_text
        )
        state.log.info(f"TG → MAX: сообщение отредактировано (max_id={max_message_id})")

    except Exception as e:
        state.log.exception(f"Ошибка редактирования TG → MAX: {e}")


@state.dp.message_reaction()
async def telegram_reaction_to_max(reaction: MessageReactionUpdated):
    """Своя реакция аккаунта в MAX — не привязка к конкретному TG-пользователю
    (см. докстринг _handle_reaction_update в relay_max_to_tg.py: та же
    асимметрия и в обратную сторону, у MAX тоже одна реакция от лица
    аккаунта). Берём первую эмодзи-реакцию из new_reaction; если её нет —
    значит все реакции сняли."""
    max_client, db_path = _resolve_route(reaction.chat.id)
    if max_client is None:
        return
    try:
        mapping = await get_max_message_id(reaction.chat.id, reaction.message_id, db_path=db_path)
        if not mapping:
            return
        max_chat_id, max_message_id = mapping

        emoji = None
        is_non_emoji_reaction = False
        for r in reaction.new_reaction:
            if isinstance(r, ReactionTypeEmoji):
                emoji = r.emoji
                break
        else:
            # new_reaction непустой, но там нет ни одного ReactionTypeEmoji —
            # значит это кастомная эмодзи-реакция (Telegram Premium,
            # ReactionTypeCustomEmoji, тысячи вариантов по custom_emoji_id)
            # или платная (ReactionTypePaid). У MAX для них нет аналога —
            # раньше это отличие вообще не проверялось, и такая реакция
            # ошибочно трактовалась как "реакцию сняли".
            is_non_emoji_reaction = bool(reaction.new_reaction)

        try:
            if is_non_emoji_reaction:
                await max_client.add_reaction(chat_id=max_chat_id, message_id=max_message_id, reaction="❤️")
            elif emoji:
                # Telegram отдаёт "❤" без variation selector (U+FE0F), а MAX
                # хранит реакции только в полной форме "❤️" (с ним) — без
                # него сервер отвечает "error.message.like.unknown.like"
                # (подтверждено через get_reactions: у самого MAX в
                # get_reactions лежит "❤️"). Один ретрай с
                # добавленным селектором, если первая попытка получила
                # именно эту ошибку — не гадаем заранее по каждому эмодзи,
                # раз затронут явно не только heart.
                try:
                    await max_client.add_reaction(chat_id=max_chat_id, message_id=max_message_id, reaction=emoji)
                except Exception as e:
                    if "unknown.like" in str(e).lower() and not emoji.endswith("️"):
                        try:
                            await max_client.add_reaction(
                                chat_id=max_chat_id, message_id=max_message_id, reaction=emoji + "️"
                            )
                        except Exception:
                            # У Telegram реакций заметно больше, чем в наборе,
                            # который принимает MAX (например "🐳") — вместо
                            # того чтобы совсем ничего не поставить, ставим
                            # нейтральное "❤️"
                            # как универсальный fallback (симметрично тому,
                            # что уже сделано для MAX → TG).
                            await max_client.add_reaction(
                                chat_id=max_chat_id, message_id=max_message_id, reaction="❤️"
                            )
                    else:
                        raise
            else:
                await max_client.remove_reaction(chat_id=max_chat_id, message_id=max_message_id)
        except Exception as e:
            # Некритично (само сообщение больше нельзя реагировать и т.п.) —
            # тихо логируем, без сообщения в топик (та же логика, что и у
            # _handle_reaction_update).
            state.log.warning(f"Не удалось передать реакцию TG → MAX (max_id={max_message_id}): {e}")
    except Exception as e:
        state.log.exception(f"Ошибка обработки реакции TG → MAX: {e}")
