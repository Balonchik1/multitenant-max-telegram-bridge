"""
Направление MAX -> Telegram: приём событий MAX и пересылка в Telegram.

Вся логика параметризована по (tg_group_id, db_path) и НЕ привязана к
конкретному клиенту — register_tenant_handlers() навешивает её на любой
pymax Client. Твоя (админская) регистрация в самом низу файла — тот же
код, что и для тенантов, просто с TG_GROUP_ID/state.DB_PATH из конфига,
как было раньше. Поведение для тебя не меняется.
"""
import asyncio
import os
import re
import tempfile
import time

import aiosqlite
from pymax import Client, Message as MaxMessage

from aiogram.types import InputMediaPhoto, InputMediaVideo, ReactionTypeEmoji

from config import TG_GROUP_ID, TG_GROUP_FLAT, ADMIN_ID
from formatters import safe_html, maybe_spoiler, fit_caption, SPOILER_LIMIT, unwrap_forward
from db_messages import (
    get_thread,
    get_tg_message_id,
    format_sender_name,
    apply_pings as _apply_pings_base,
    get_last_seen_at,
    set_last_seen_at,
    get_recent_messages_for_reaction_poll,
    get_known_reaction,
    set_known_reaction,
)
from mentions import apply_max_mentions
from relay_tg_to_max import get_or_create_topic
from media import safe_remove
import state

# --- Монкипатч pymax: реальный MAX шлёт messageId в reaction_update как
# число, а pymax 2.4.1 объявляет ReactionUpdateEvent.message_id: str —
# из-за этого dispatch() падал с pydantic ValidationError ещё ДО вызова
# нашего обработчика, и любая реакция, поставленная в самом MAX, никогда
# не долетала ("Failed to dispatch inbound frame" на каждую реакцию).
# Патчим точечно только имя, которое реально
# резолвится в pymax.dispatch.mapping.map() (bare-name lookup в глобалах
# этого модуля) — остальной pymax не трогаем.
import pymax.dispatch.mapping as _pymax_mapping
from pymax.types.events.reaction import ReactionUpdateEvent as _BaseReactionUpdateEvent


class _PatchedReactionUpdateEvent(_BaseReactionUpdateEvent):
    message_id: int | str


_pymax_mapping.ReactionUpdateEvent = _PatchedReactionUpdateEvent


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _render_poll_text(attach) -> str:
    """POLL вложение (pymax PollAttachment) -> HTML-текст для отправки в TG.
    Само голосование недоступно из TG — только вопрос и варианты ответа."""
    title = (
        getattr(attach, "title", None)
        or (attach.get("title") if isinstance(attach, dict) else None)
        or "Опрос"
    )
    answers = (
        getattr(attach, "answers", None)
        or (attach.get("answers") if isinstance(attach, dict) else None)
        or []
    )
    lines = [f"📊 <b>Опрос:</b> {safe_html(str(title))}"]
    for a in answers:
        text_a = getattr(a, "text", None) or (a.get("text") if isinstance(a, dict) else None)
        if text_a:
            lines.append(f"— {safe_html(str(text_a))}")
    lines.append("<i>Голосование доступно только в самом MAX</i>")
    return "\n".join(lines)


def _render_contact_text(attach) -> str:
    """CONTACT вложение (pymax ContactAttachment) -> HTML-текст для TG.
    У pymax нет номера телефона в этой модели — показываем только имя."""
    name = getattr(attach, "name", None) or (attach.get("name") if isinstance(attach, dict) else None)
    if not name:
        first = (
            getattr(attach, "first_name", None)
            or (attach.get("first_name") if isinstance(attach, dict) else None)
            or ""
        )
        last = (
            getattr(attach, "last_name", None)
            or (attach.get("last_name") if isinstance(attach, dict) else None)
            or ""
        )
        name = f"{first} {last}".strip() or "Контакт"
    return f"📇 <b>Контакт:</b> {safe_html(str(name))}"


def _render_call_text(attach) -> str:
    """CALL вложение (pymax CallAttachment) -> текст для TG. Это отдельный,
    структурированный тип вложения для ЛИЧНЫХ звонков (в отличие от
    группового звонка, который приходит текстовым системным CONTROL-событием
    и уже обрабатывается веткой event == "system" ниже) — раньше падал в
    "неизвестный тип вложения" и терялся молча."""
    hangup = (
        getattr(attach, "hangup_type", None)
        or (attach.get("hangup_type") if isinstance(attach, dict) else None)
    )
    # str(enum_member) даёт "HangupType.MISSED", а не "MISSED" (Enum
    # переопределяет __str__) — берём .value явно.
    hangup = str(getattr(hangup, "value", hangup)).upper() if hangup else ""
    duration = (
        getattr(attach, "duration", None)
        or (attach.get("duration") if isinstance(attach, dict) else None)
        or 0
    )
    call_type = (
        getattr(attach, "call_type", None)
        or (attach.get("call_type") if isinstance(attach, dict) else None)
    )
    call_type = str(getattr(call_type, "value", call_type)).upper() if call_type else ""

    kind = "🎥 Видеозвонок" if call_type == "VIDEO" else "📞 Звонок"
    if hangup == "MISSED":
        return f"{kind} — пропущен"
    elif hangup == "REJECTED":
        return f"{kind} — отклонён"
    elif hangup == "CANCELED":
        return f"{kind} — отменён"
    elif duration:
        mins, secs = divmod(int(duration), 60)
        return f"{kind} — {mins}:{secs:02d}"
    return kind


async def _handle_max_message(message: MaxMessage, client: Client, *, tg_group_id: int, db_path: str):
    # Свои обычные сообщения игнорируем, системные CONTROL — пропускаем
    is_control = False
    if message.attaches:
        for a in message.attaches:
            t = getattr(a, "type", None)
            t = t.value if hasattr(t, "value") else t
            if str(t).upper() == "CONTROL":
                is_control = True
                break

    if message.sender == client.me.contact.id and not is_control:
        return

    print("ПОЛУЧЕНО СОБЫТИЕ MAX")
    # Ключ дедупликации включает id аккаунта: если один и тот же MAX-чат
    # шарится между несколькими твоими аккаунтами (админ + tenant состоят
    # в одной MAX-группе), каждый клиент честно получает СВОЁ on_message с
    # одинаковым (chat_id, message.id) — без profile_id в ключе второй
    # клиент видел бы "уже обработано" и молча терял своё сообщение
    # (сообщение долетало только до одного из двух аккаунтов, никогда
    # до обоих).
    mid_key = (client.me.contact.id, message.chat_id, message.id)
    now = time.time()
    last = state._recent_max_msgs.get(mid_key)
    if last and now - last < 5:
        return
    state._recent_max_msgs[mid_key] = now
    if len(state._recent_max_msgs) > 500:
        cutoff = now - 60
        for k in list(state._recent_max_msgs.keys()):
            if state._recent_max_msgs[k] < cutoff:
                del state._recent_max_msgs[k]
    print("MAX:", message.sender, message.text)

    # Локально затеняем apply_pings своей db_path — иначе пинги tenant'ов
    # читались бы из ТВОЕЙ таблицы ping_map (тот же класс бага, что и с
    # упоминаниями выше).
    async def apply_pings(text):
        return await _apply_pings_base(text, db_path=db_path)

    mapping = await get_thread(message.chat_id, db_path=db_path, default_tg_group_id=tg_group_id)
    if mapping:
        target_chat_id = mapping["telegram_chat_id"]
        target_thread_id = mapping["telegram_thread_id"]  # может быть None
    else:
        thread_id = await get_or_create_topic(message, client, tg_group_id=tg_group_id, db_path=db_path)
        if not thread_id:
            return
        target_chat_id = tg_group_id
        target_thread_id = thread_id

    # Для обычных групп топик обязателен
    if target_chat_id != TG_GROUP_FLAT and target_thread_id is None:
        return

    # Получаем имя отправителя (особенно важно для групп)
    prefix = ""
    # message.sender is None у постов канала (ChatType.CHANNEL) — у них нет
    # индивидуального автора, MAX его просто не присылает. Без этой проверки
    # format_sender_name(client, None, ...) не находил юзера и подставлял
    # бессмысленную заглушку "Unknown None" в префикс.
    if message.chat_id < 0 and message.sender is not None:
        try:
            name = await format_sender_name(client, message.sender, db_path=db_path)
            prefix = f"<b>{safe_html(name)}</b>\n"
        except Exception as e:
            state.log.warning(f"Имя отправителя: {e}")
            prefix = f"<b>{message.sender}</b>\n"

    data = message.model_dump()

    reply_to_tg_id = None
    if data.get("link") and data["link"].get("type") == "REPLY":
        replied_msg = data["link"].get("message", {})
        replied_max_id = (
            replied_msg.get("id")
            or data["link"].get("messageId")
            or data["link"].get("message_id")
        )
        if replied_max_id:
            mapping = await get_tg_message_id(message.chat_id, replied_max_id, db_path=db_path)
            if mapping:
                reply_to_tg_id = mapping[2]  # tg_message_id

    is_forward, src_msg, src_chat_id, forward_text, attaches, origin_name = unwrap_forward(message)

    # USER_MENTION из MAX → @tg (если есть /link). Уведомление про
    # непривязанное упоминание — только тебе (notify_id=None у tenant'ов):
    # фича сделана под твой конкретный workflow (flat-группа с коллегами
    # без MAX), другим пользователям она не нужна.
    src_elements = getattr(src_msg, "elements", None) or getattr(message, "elements", None)
    forward_text = await apply_max_mentions(
        forward_text or "",
        src_elements,
        client,
        message.chat_id,
        db_path=db_path,
        notify_id=None if tg_group_id in state.tenant_group_map else ADMIN_ID,
    )
    fwd_line = ""
    if is_forward:
        who = origin_name
        if who and str(who).isdigit():
            try:
                who = await format_sender_name(client, int(who), db_path=db_path)
            except Exception:
                pass
        if not who:
            who = "неизвестный"
        fwd_line = f"🔁 <b>Переслано от {safe_html(str(who))}</b>\n"

    if attaches:
        media_items = []
        other_tasks = []
        share_done = False
        for attach in attaches:
            try:
                if hasattr(attach, "type"):
                    attach_type = attach.type.value if hasattr(attach.type, "value") else str(attach.type)
                else:
                    attach_type = attach.get("_type") or attach.get("type")
                attach_type = str(attach_type).upper()
                if attach_type == "SHARE":
                    if share_done:
                        continue
                    share_done = True

                # Проверка размера
                file_size = (
                    getattr(attach, "size", None)
                    or (attach.get("size") if isinstance(attach, dict) else None)
                    or 0
                )
                if file_size and file_size > state.MAX_FILE_SIZE:
                    state.log.warning(
                        f"Файл > 50 МБ пропущен: chat_id={message.chat_id} size={file_size}"
                    )
                    # У видео есть свой fallback (превью) чуть ниже, а у
                    # остальных типов (в первую очередь FILE) вложение просто
                    # молча пропадало без единого следа для пользователя.
                    if attach_type not in ("VIDEO",):
                        file_name = (
                            getattr(attach, "name", None)
                            or (attach.get("name") if isinstance(attach, dict) else None)
                            or "файл"
                        )
                        kwargs = {
                            "chat_id": target_chat_id,
                            "text": prefix + fwd_line + f"📎 «{safe_html(file_name)}» больше 50 МБ — бот не может его переслать (лимит Telegram).",
                            "parse_mode": "HTML",
                        }
                        if target_thread_id is not None:
                            kwargs["message_thread_id"] = target_thread_id
                        if reply_to_tg_id:
                            kwargs["reply_to_message_id"] = reply_to_tg_id
                        other_tasks.append({
                            "method": "send_message",
                            "kwargs": kwargs,
                            "max_chat_id": message.chat_id,
                            "max_message_id": message.id,
                            "body_text": None,
                            "max_sender_id": message.sender,
                            "db_path": db_path,
                        })
                    continue

                # === ФОТО ===
                if attach_type == "PHOTO":
                    photo_url = getattr(attach, "base_url", None) or attach.get("baseUrl") or attach.get("base_url")
                    if photo_url:
                        media_items.append({"type": "photo", "url": photo_url})

                # === ВИДЕО ===
                elif attach_type == "VIDEO":
                    state.log.info(
                        f"VIDEO forward={is_forward} src_chat={src_chat_id} "
                        f"src_id={getattr(src_msg, 'id', None)} cur_id={message.id}"
                    )
                    try:
                        video_url = None
                        thumbnail = (
                            getattr(attach, "thumbnail", None)
                            or (attach.get("thumbnail") if isinstance(attach, dict) else None)
                        )
                        # Как и video_url ниже — MAX иногда отдаёт URL без схемы,
                        # Telegram такой скачать не может ("failed to get HTTP
                        # URL content").
                        if thumbnail and not thumbnail.startswith("http"):
                            thumbnail = "https://" + thumbnail

                        video_id = getattr(attach, "video_id", None) or attach.get("video_id") or attach.get("videoId")
                        if video_id and message.chat_id and message.id:
                            try:
                                video_info = await client.get_video_by_id(
                                    chat_id=src_chat_id or message.chat_id,
                                    message_id=getattr(src_msg, "id", None) or message.id,
                                    video_id=video_id,
                                )
                                if video_info:
                                    raw_url = getattr(video_info, "url", None)
                                    if isinstance(raw_url, list):
                                        raw_url = raw_url[0] if raw_url else None
                                    if isinstance(raw_url, str) and raw_url:
                                        video_url = raw_url if raw_url.startswith("http") else "https://" + raw_url
                            except Exception as e:
                                state.log.warning(f"get_video_by_id не удалось: {e}")

                        file_size = (
                            getattr(attach, "size", None)
                            or (attach.get("size") if isinstance(attach, dict) else None)
                            or 0
                        )
                        size_known = bool(file_size)
                        # too_big — только когда размер ТОЧНО известен и
                        # превышает лимит: тогда пробовать нет смысла, сразу
                        # превью. Если размер неизвестен — раньше это тоже
                        # трактовалось как "слишком большое" и всегда шло
                        # превью, даже когда видео реально маленькое (MAX не
                        # всегда присылает размер). Теперь пробуем отправить
                        # целиком, а на неудачу именно из-за размера/скачивания
                        # (_is_content_send_error в tg_worker) откатываемся на
                        # превью уже там — см. "fallback_thumbnail" ниже.
                        too_big = size_known and file_size > state.MAX_FILE_SIZE

                        if video_url and not too_big:
                            media_items.append({
                                "type": "video",
                                "url": video_url,
                                # Только когда размер не подтверждён — если он
                                # ТОЧНО маленький, откатываться не на что: любая
                                # ошибка тут не про размер.
                                "fallback_thumbnail": thumbnail if not size_known else None,
                            })
                        elif thumbnail:
                            # превью: нет URL, ошибка get_video, размер неизвестен, или файл > 50 МБ
                            body = await apply_pings(forward_text or message.text or "")
                            caption = prefix + fwd_line
                            if body:
                                caption += maybe_spoiler(safe_html(body)) + "\n"
                            if size_known and too_big:
                                caption += "<i>🎬 Видео больше 50 МБ — только превью</i>"
                            elif not size_known:
                                caption += "<i>🎬 Видео — только превью (не удалось проверить размер)</i>"
                            else:
                                caption += "<i>🎬 Видео (превью)</i>"

                            # не длиннее 1024
                            if len(caption) > 1024:
                                caption = caption[:1023] + "…"

                            kwargs = {
                                "chat_id": target_chat_id,
                                "photo": thumbnail,
                                "caption": caption,
                                "parse_mode": "HTML",
                            }
                            if target_thread_id is not None:
                                kwargs["message_thread_id"] = target_thread_id
                            if reply_to_tg_id:
                                kwargs["reply_to_message_id"] = reply_to_tg_id
                            other_tasks.append({
                                "method": "send_photo",
                                "kwargs": kwargs,
                                "max_chat_id": message.chat_id,
                                "max_message_id": message.id,
                                "body_text": (forward_text or "") or None,
                                "max_sender_id": message.sender,
                                "db_path": db_path,
                            })
                        else:
                            kwargs = {
                                "chat_id": target_chat_id,
                                "text": prefix
                                + fwd_line
                                + ("🎬 Видео больше 50 МБ" if too_big else "🎬 Видео (не удалось получить)"),
                                "parse_mode": "HTML",
                            }
                            if target_thread_id is not None:
                                kwargs["message_thread_id"] = target_thread_id
                            if reply_to_tg_id:
                                kwargs["reply_to_message_id"] = reply_to_tg_id
                            other_tasks.append({
                                "method": "send_message",
                                "kwargs": kwargs,
                                "max_chat_id": message.chat_id,
                                "max_message_id": message.id,
                                "body_text": (forward_text or "") or None,
                                "max_sender_id": message.sender,
                                "db_path": db_path,
                            })

                    except Exception as e:
                        state.log.error(f"Ошибка видео: {e}")
                        kwargs = {
                            "chat_id": target_chat_id,
                            "text": prefix + "🎬 Видео (ошибка загрузки)",
                            "parse_mode": "HTML"
                        }
                        if target_thread_id is not None:
                            kwargs["message_thread_id"] = target_thread_id
                        if reply_to_tg_id:
                            kwargs["reply_to_message_id"] = reply_to_tg_id
                        other_tasks.append({
                            "method": "send_message",
                            "kwargs": kwargs,
                            "max_chat_id": message.chat_id,
                            "max_message_id": message.id,
                            "body_text": (forward_text or "") or None,
                            "max_sender_id": message.sender,
                            "db_path": db_path,
                        })

                # === ФАЙЛ / ДОКУМЕНТ ===
                elif attach_type == "FILE":
                    state.log.info(f"Обрабатываю FILE: {attach}")
                    try:
                        file_id = getattr(attach, "file_id", None) or attach.get("file_id") or attach.get("fileId")
                        file_name = getattr(attach, "name", None) or attach.get("name") or "file"
                        state.log.info(f"file_id={file_id}, name={file_name}")

                        if file_id and message.chat_id and message.id:
                            file_info = await client.get_file_by_id(
                                chat_id=src_chat_id or message.chat_id,
                                message_id=getattr(src_msg, "id", None) or message.id,
                                file_id=file_id,
                            )
                            state.log.info(f"file_info={file_info}")

                            if file_info and getattr(file_info, "url", None):
                                import aiohttp
                                from aiogram.types import FSInputFile

                                tmp_path = None
                                try:
                                    async with aiohttp.ClientSession() as session:
                                        async with session.get(file_info.url) as resp:
                                            if resp.status != 200:
                                                raise Exception(f"HTTP {resp.status}")
                                            ext = os.path.splitext(file_name)[1] or ".bin"
                                            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                                                tmp_path = tmp.name
                                                tmp.write(await resp.read())

                                    # Формируем подпись без названия файла
                                    caption_parts = []
                                    if prefix:
                                        caption_parts.append(prefix.strip())
                                    if is_forward:
                                        caption_parts.append(fwd_line.strip())
                                    if forward_text:
                                        body_plain = await apply_pings(forward_text or "")
                                        # prefix + optional forward уже в caption_parts
                                        reserved = len(prefix or "") + (30 if is_forward else 0) + len("<tg-spoiler></tg-spoiler>")
                                        max_body = max(1, 1024 - reserved)
                                        if len(body_plain) > max_body:
                                            body_plain = body_plain[: max_body - 1] + "…"
                                        caption_parts.append(maybe_spoiler(body_plain))

                                    caption = "\n".join(caption_parts) if caption_parts else None

                                    kwargs = {
                                        "chat_id": target_chat_id,
                                        "document": FSInputFile(tmp_path, filename=file_name),
                                        "caption": caption,
                                        "parse_mode": "HTML" if caption else None
                                    }
                                    if target_thread_id is not None:
                                        kwargs["message_thread_id"] = target_thread_id
                                    if reply_to_tg_id:
                                        kwargs["reply_to_message_id"] = reply_to_tg_id

                                    other_tasks.append({
                                        "method": "send_document",
                                        "kwargs": kwargs,
                                        "max_chat_id": message.chat_id,
                                        "max_message_id": message.id,
                                        "tmp_path": tmp_path,
                                        "body_text": (forward_text or "") or None,
                                        "max_sender_id": message.sender,
                                        "db_path": db_path,
                                    })
                                except Exception as e:
                                    state.log.error(f"Не удалось скачать файл: {e}")
                                    if tmp_path:
                                        await safe_remove(tmp_path)
                                    kwargs = {
                                        "chat_id": target_chat_id,
                                        "text": prefix + f"📎 Файл: {safe_html(file_name)} (ошибка скачивания)",
                                        "parse_mode": "HTML"
                                    }
                                    if target_thread_id is not None:
                                        kwargs["message_thread_id"] = target_thread_id
                                    if reply_to_tg_id:
                                        kwargs["reply_to_message_id"] = reply_to_tg_id
                                    other_tasks.append({
                                        "method": "send_message",
                                        "kwargs": kwargs,
                                        "max_chat_id": message.chat_id,
                                        "max_message_id": message.id,
                                        "body_text": (forward_text or "") or None,
                                        "max_sender_id": message.sender,
                                        "db_path": db_path,
                                    })
                            else:
                                state.log.warning("Не удалось получить url файла")
                                kwargs = {
                                    "chat_id": target_chat_id,
                                    "text": prefix + f"📎 Файл: {safe_html(file_name)} (не удалось получить ссылку)",
                                    "parse_mode": "HTML"
                                }
                                if target_thread_id is not None:
                                    kwargs["message_thread_id"] = target_thread_id
                                if reply_to_tg_id:
                                    kwargs["reply_to_message_id"] = reply_to_tg_id
                                other_tasks.append({
                                    "method": "send_message",
                                    "kwargs": kwargs,
                                    "max_chat_id": message.chat_id,
                                    "max_message_id": message.id,
                                    "body_text": (forward_text or "") or None,
                                    "max_sender_id": message.sender,
                                    "db_path": db_path,
                                })
                        else:
                            state.log.warning("Нет file_id или message.id")
                            kwargs = {
                                "chat_id": target_chat_id,
                                "text": prefix + f"📎 Пользователь отправил файл: {safe_html(file_name)}",
                                "parse_mode": "HTML"
                            }
                            if target_thread_id is not None:
                                kwargs["message_thread_id"] = target_thread_id
                            if reply_to_tg_id:
                                kwargs["reply_to_message_id"] = reply_to_tg_id
                            other_tasks.append({
                                "method": "send_message",
                                "kwargs": kwargs,
                                "max_chat_id": message.chat_id,
                                "max_message_id": message.id,
                                "body_text": (forward_text or "") or None,
                                "max_sender_id": message.sender,
                                "db_path": db_path,
                            })
                    except Exception as e:
                        state.log.error(f"Ошибка файла: {e}")
                        kwargs = {
                            "chat_id": target_chat_id,
                            "text": prefix + "📎 Файл (ошибка загрузки)",
                            "parse_mode": "HTML"
                        }
                        if target_thread_id is not None:
                            kwargs["message_thread_id"] = target_thread_id
                        other_tasks.append({
                            "method": "send_message",
                            "kwargs": kwargs,
                            "max_chat_id": message.chat_id,
                            "max_message_id": message.id,
                            "body_text": (forward_text or "") or None,
                            "max_sender_id": message.sender,
                            "db_path": db_path,
                        })

                # === СТИКЕР ===
                elif attach_type == "STICKER":
                    sticker_url = getattr(attach, "url", None) or attach.get("url")

                    tags = getattr(attach, "tags", None) or (
                        attach.get("tags") if isinstance(attach, dict) else None
                    )
                    raw = ""
                    if isinstance(tags, (list, tuple)) and tags:
                        raw = str(tags[0])
                    elif tags:
                        raw = str(tags).strip()

                    # Берём только первый эмодзи из строки
                    emoji = ""
                    if raw:
                        m = re.search(
                            r"(?:[\U0001F1E0-\U0001F1FF]{2})"
                            r"|[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F600-\U0001F64F\U0001F900-\U0001F9FF]"
                            r"(?:\U0000FE0F)?"
                            r"(?:\U0000200D[\U0001F300-\U0001FAFF\U00002600-\U000027BF](?:\U0000FE0F)?)*",
                            raw,
                        )
                        emoji = m.group(0) if m else raw[0]

                    # Без «Переслано» — только префикс имени (в группах) + Стикер + 1 эмодзи
                    caption = (prefix + f"Стикер {emoji}").strip()
                    caption = fit_caption(caption)

                    kwargs = {
                        "chat_id": target_chat_id,
                    }
                    if target_thread_id is not None:
                        kwargs["message_thread_id"] = target_thread_id
                    if reply_to_tg_id:
                        kwargs["reply_to_message_id"] = reply_to_tg_id

                    if sticker_url:
                        kwargs["photo"] = sticker_url
                        kwargs["caption"] = caption
                        kwargs["parse_mode"] = "HTML"
                        other_tasks.append({
                            "method": "send_photo",
                            "kwargs": kwargs,
                            "max_chat_id": message.chat_id,
                            "max_message_id": message.id,
                            "body_text": f"Стикер {emoji}",
                            "max_sender_id": message.sender,
                            "db_path": db_path,
                        })
                    else:
                        kwargs["text"] = caption
                        kwargs["parse_mode"] = "HTML"
                        other_tasks.append({
                            "method": "send_message",
                            "kwargs": kwargs,
                            "max_chat_id": message.chat_id,
                            "max_message_id": message.id,
                            "body_text": f"Стикер {emoji}",
                            "max_sender_id": message.sender,
                            "db_path": db_path,
                        })

                # === ГОЛОСОВОЕ / АУДИО ===
                elif attach_type == "AUDIO":
                    audio_url = getattr(attach, "url", None) or attach.get("url")
                    duration = getattr(attach, "duration", None) or attach.get("duration")
                    kwargs = {
                        "chat_id": target_chat_id,
                    }
                    if target_thread_id is not None:
                        kwargs["message_thread_id"] = target_thread_id
                    if reply_to_tg_id:
                        kwargs["reply_to_message_id"] = reply_to_tg_id

                    if audio_url:
                        kwargs["voice"] = audio_url
                        body_plain = await apply_pings(forward_text or "")
                        prefix_part = prefix + fwd_line
                        spoiler_tags = len("<tg-spoiler></tg-spoiler>") if len(body_plain) > SPOILER_LIMIT else 0
                        max_body = max(1, 1024 - len(prefix_part) - spoiler_tags)
                        if len(body_plain) > max_body:
                            body_plain = body_plain[: max_body - 1] + "…"
                        kwargs["caption"] = prefix_part + maybe_spoiler(body_plain)
                        kwargs["parse_mode"] = "HTML"
                        kwargs["duration"] = duration
                        other_tasks.append({
                            "method": "send_voice",
                            "kwargs": kwargs,
                            "max_chat_id": message.chat_id,
                            "max_message_id": message.id,
                            "body_text": (forward_text or "") or None,
                            "max_sender_id": message.sender,
                            "db_path": db_path,
                        })
                    else:
                        kwargs["text"] = prefix + fwd_line + "🎤 Пользователь отправил голосовое/аудио"
                        kwargs["parse_mode"] = "HTML"
                        other_tasks.append({
                            "method": "send_message",
                            "kwargs": kwargs,
                            "max_chat_id": message.chat_id,
                            "max_message_id": message.id,
                            "body_text": (forward_text or "") or None,
                            "max_sender_id": message.sender,
                            "db_path": db_path,
                        })

                # === ССЫЛКА / SHARE ===
                elif attach_type == "SHARE":
                    state.log.info(f"Обрабатываю SHARE: {attach}")

                    url = (
                        getattr(attach, "url", None)
                        or (attach.get("url") if isinstance(attach, dict) else None)
                        or getattr(attach, "link", None)
                        or (attach.get("link") if isinstance(attach, dict) else None)
                    )
                    url = str(url).strip() if url else ""

                    text_parts = []
                    if prefix:
                        text_parts.append(prefix.strip())
                    if is_forward:
                        text_parts.append(fwd_line.strip())

                    body = (await apply_pings(forward_text or "")).strip()

                    # Если текст уже содержит ссылку — не дублируем
                    if body:
                        text_parts.append(maybe_spoiler(body))
                        if url and url not in body:
                            text_parts.append(url)
                    elif url:
                        text_parts.append(url)
                    else:
                        text_parts.append("🔗 Ссылка")

                    final_text = "\n".join(text_parts)

                    kwargs = {
                        "chat_id": target_chat_id,
                        "text": final_text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    }
                    if target_thread_id is not None:
                        kwargs["message_thread_id"] = target_thread_id
                    if reply_to_tg_id:
                        kwargs["reply_to_message_id"] = reply_to_tg_id

                    other_tasks.append({
                        "method": "send_message",
                        "kwargs": kwargs,
                        "max_chat_id": message.chat_id,
                        "max_message_id": message.id,
                        "body_text": (forward_text or "") or None,
                        "max_sender_id": message.sender,
                        "db_path": db_path,
                    })

                # === СИСТЕМНЫЕ СОБЫТИЯ ===
                elif attach_type == "CONTROL":
                    event = (
                        getattr(attach, "event", None)
                        or (attach.get("event") if isinstance(attach, dict) else None)
                        or ""
                    )
                    event = str(event).lower()

                    async def get_name(user_id):
                        if not user_id:
                            return "Кто-то"
                        try:
                            u = await client.get_user(user_id)
                            if u and u.names:
                                n = u.names[0]
                                if n.first_name and n.last_name:
                                    return f"{n.first_name} {n.last_name}".strip()
                                return n.first_name or n.name or str(user_id)
                        except Exception:
                            pass
                        return str(user_id)

                    def get_extra(key, default=None):
                        val = getattr(attach, key, None)
                        if val is not None:
                            return val
                        if isinstance(attach, dict):
                            return attach.get(key, default)
                        if hasattr(attach, "model_extra") and attach.model_extra:
                            return attach.model_extra.get(key, default)
                        return default

                    if event == "leave":
                        who = await get_name(message.sender)
                        text = f"<blockquote><i>🚪 <b>{safe_html(who)}</b> покинул(а) группу</i></blockquote>"
                    elif event == "add":
                        adder = await get_name(message.sender)
                        user_ids = get_extra("userIds") or get_extra("user_ids") or []
                        names = [await get_name(uid) for uid in user_ids] if user_ids else []
                        who_added = ", ".join(names) if names else "пользователь"
                        text = (
                            f"<blockquote><i>➕ <b>{safe_html(adder)}</b> добавил(а) "
                            f"<b>{safe_html(who_added)}</b> в группу</i></blockquote>"
                        )
                    elif event == "remove":
                        remover = await get_name(message.sender)
                        removed_id = get_extra("userId") or get_extra("user_id")
                        removed = await get_name(removed_id)
                        text = (
                            f"<blockquote><i>🚫 <b>{safe_html(remover)}</b> исключил(а) "
                            f"<b>{safe_html(removed)}</b> из группы</i></blockquote>"
                        )
                    elif event == "joinbylink":
                        who = await get_name(get_extra("userId") or get_extra("user_id") or message.sender)
                        text = f"<blockquote><i>🔗 <b>{safe_html(who)}</b> присоединился(ась) по ссылке</i></blockquote>"
                    elif event == "system":
                        sys_text = (
                            getattr(attach, "message", None)
                            or getattr(attach, "shortMessage", None)
                            or (attach.get("message") if isinstance(attach, dict) else None)
                            or (attach.get("shortMessage") if isinstance(attach, dict) else None)
                            or "Системное событие"
                        )
                        sys_text = str(sys_text)
                        low = sys_text.lower()
                        if "звонок" in low and ("начал" in low or "начат" in low):
                            text = f"<blockquote><i>📞 {safe_html(sys_text)}</i></blockquote>"
                        elif "звонок" in low and ("заверш" in low or "оконч" in low):
                            text = f"<blockquote><i>📞 {safe_html(sys_text)}</i></blockquote>"
                        else:
                            text = f"<blockquote><i>ℹ️ {safe_html(sys_text)}</i></blockquote>"
                    else:
                        text = f"ℹ️ Системное событие группы: <code>{safe_html(event)}</code>"

                    kwargs = {
                        "chat_id": target_chat_id,
                        "text": text,
                        "parse_mode": "HTML"
                    }
                    if target_thread_id is not None:
                        kwargs["message_thread_id"] = target_thread_id
                    if reply_to_tg_id:
                        kwargs["reply_to_message_id"] = reply_to_tg_id

                    other_tasks.append({
                        "method": "send_message",
                        "kwargs": kwargs,
                        "max_chat_id": message.chat_id,
                        "max_message_id": message.id,
                        "body_text": (forward_text or "") or None,
                        "max_sender_id": message.sender,
                        "db_path": db_path,
                    })

                # === ОПРОС ===
                elif attach_type == "POLL":
                    text_out = _render_poll_text(attach)

                    kwargs = {"chat_id": target_chat_id, "text": prefix + text_out, "parse_mode": "HTML"}
                    if target_thread_id is not None:
                        kwargs["message_thread_id"] = target_thread_id
                    if reply_to_tg_id:
                        kwargs["reply_to_message_id"] = reply_to_tg_id
                    other_tasks.append({
                        "method": "send_message",
                        "kwargs": kwargs,
                        "max_chat_id": message.chat_id,
                        "max_message_id": message.id,
                        # body_text используется при пометке удаления
                        # (_handle_message_delete), которая сама заново
                        # экранирует через safe_html() — храним без HTML-тегов,
                        # иначе при удалении показались бы буквальные <b>/<i>.
                        "body_text": _strip_html_tags(text_out),
                        "max_sender_id": message.sender,
                        "db_path": db_path,
                    })

                # === КОНТАКТ ===
                elif attach_type == "CONTACT":
                    text_out = _render_contact_text(attach)

                    kwargs = {"chat_id": target_chat_id, "text": prefix + text_out, "parse_mode": "HTML"}
                    if target_thread_id is not None:
                        kwargs["message_thread_id"] = target_thread_id
                    if reply_to_tg_id:
                        kwargs["reply_to_message_id"] = reply_to_tg_id
                    other_tasks.append({
                        "method": "send_message",
                        "kwargs": kwargs,
                        "max_chat_id": message.chat_id,
                        "max_message_id": message.id,
                        "body_text": _strip_html_tags(text_out),
                        "max_sender_id": message.sender,
                        "db_path": db_path,
                    })

                # === ЗВОНОК ===
                elif attach_type == "CALL":
                    text_out = _render_call_text(attach)

                    kwargs = {"chat_id": target_chat_id, "text": prefix + text_out, "parse_mode": "HTML"}
                    if target_thread_id is not None:
                        kwargs["message_thread_id"] = target_thread_id
                    if reply_to_tg_id:
                        kwargs["reply_to_message_id"] = reply_to_tg_id
                    other_tasks.append({
                        "method": "send_message",
                        "kwargs": kwargs,
                        "max_chat_id": message.chat_id,
                        "max_message_id": message.id,
                        "body_text": text_out,
                        "max_sender_id": message.sender,
                        "db_path": db_path,
                    })

                else:
                    state.log.warning(f"Неизвестный тип вложения: {attach_type}")

            except Exception as e:
                state.log.error(f"Ошибка обработки вложения: {e}")

        # ----- Отправка альбома (2+ медиа) -----
        if len(media_items) >= 2:
            body_plain = await apply_pings(forward_text or message.text or "")
            prefix_part = prefix + fwd_line

            # запас на теги спойлера, если текст длинный
            spoiler_tags = len("<tg-spoiler></tg-spoiler>") if len(body_plain) > SPOILER_LIMIT else 0
            max_body = 1024 - len(prefix_part) - spoiler_tags
            if max_body < 1:
                max_body = 1
            if len(body_plain) > max_body:
                body_plain = body_plain[: max_body - 1] + "…"

            body = maybe_spoiler(body_plain)
            caption = prefix_part + body

            # Видео с неподтверждённым размером выносим из атомарной группы:
            # send_media_group либо уходит целиком, либо падает целиком, и
            # непонятно, какой именно элемент виноват. Вместо пересборки
            # группы — такое видео просто идёт отдельным сообщением со своим
            # собственным откатом на превью (та же логика, что и у
            # одиночного видео ниже, elif len(media_items) == 1), а
            # остальные (точно безопасные) элементы уходят одним альбомом
            # как раньше.
            safe_items = [it for it in media_items if not it.get("fallback_thumbnail")]
            risky_items = [it for it in media_items if it.get("fallback_thumbnail")]
            caption_used = False

            if len(safe_items) >= 2:
                media_group = []
                for i, item in enumerate(safe_items):
                    cls = InputMediaPhoto if item["type"] == "photo" else InputMediaVideo
                    media_group.append(cls(
                        media=item["url"],
                        caption=caption if i == 0 else None,
                        parse_mode="HTML" if i == 0 else None,
                    ))
                caption_used = True

                kwargs = {"chat_id": target_chat_id, "media": media_group}
                if target_thread_id is not None:
                    kwargs["message_thread_id"] = target_thread_id
                if reply_to_tg_id:
                    kwargs["reply_to_message_id"] = reply_to_tg_id

                await state.tg_queue.put({
                    "method": "send_media_group",
                    "kwargs": kwargs,
                    "max_chat_id": message.chat_id,
                    "max_message_id": message.id,
                    "body_text": (forward_text or message.text or "") or None,
                    "max_sender_id": message.sender,
                    "db_path": db_path,
                })
            elif len(safe_items) == 1:
                item = safe_items[0]
                kwargs = {
                    "chat_id": target_chat_id,
                    ("photo" if item["type"] == "photo" else "video"): item["url"],
                    "caption": caption,
                    "parse_mode": "HTML",
                }
                if target_thread_id is not None:
                    kwargs["message_thread_id"] = target_thread_id
                if reply_to_tg_id:
                    kwargs["reply_to_message_id"] = reply_to_tg_id
                caption_used = True

                await state.tg_queue.put({
                    "method": "send_photo" if item["type"] == "photo" else "send_video",
                    "kwargs": kwargs,
                    "max_chat_id": message.chat_id,
                    "max_message_id": message.id,
                    "body_text": (forward_text or message.text or "") or None,
                    "max_sender_id": message.sender,
                    "db_path": db_path,
                })

            for item in risky_items:
                item_caption = caption if not caption_used else ((prefix + fwd_line).strip() or None)
                caption_used = True
                kwargs = {
                    "chat_id": target_chat_id,
                    "video": item["url"],
                    "caption": item_caption,
                    "parse_mode": "HTML" if item_caption else None,
                }
                if target_thread_id is not None:
                    kwargs["message_thread_id"] = target_thread_id
                if reply_to_tg_id:
                    kwargs["reply_to_message_id"] = reply_to_tg_id

                task = {
                    "method": "send_video",
                    "kwargs": kwargs,
                    "max_chat_id": message.chat_id,
                    "max_message_id": message.id,
                    "body_text": (forward_text or message.text or "") or None,
                    "max_sender_id": message.sender,
                    "db_path": db_path,
                }
                fallback_kwargs = {
                    "chat_id": target_chat_id,
                    "photo": item["fallback_thumbnail"],
                    # Точную причину (размер или что-то другое) допишет
                    # tg_worker перед самой отправкой — там уже известна
                    # настоящая ошибка, а не только сам факт неудачи.
                    "caption": item_caption + "\n" if item_caption else prefix + fwd_line,
                    "parse_mode": "HTML",
                }
                if target_thread_id is not None:
                    fallback_kwargs["message_thread_id"] = target_thread_id
                if reply_to_tg_id:
                    fallback_kwargs["reply_to_message_id"] = reply_to_tg_id
                task["fallback"] = {"method": "send_photo", "kwargs": fallback_kwargs}

                await state.tg_queue.put(task)

        elif len(media_items) == 1:
            # Одно медиа
            item = media_items[0]
            body_plain = await apply_pings(forward_text or message.text or "")
            prefix_part = prefix + fwd_line

            # запас на теги спойлера, если текст длинный
            spoiler_tags = len("<tg-spoiler></tg-spoiler>") if len(body_plain) > SPOILER_LIMIT else 0
            max_body = 1024 - len(prefix_part) - spoiler_tags
            if max_body < 1:
                max_body = 1
            if len(body_plain) > max_body:
                body_plain = body_plain[: max_body - 1] + "…"

            body = maybe_spoiler(body_plain)
            caption = prefix_part + body
            if item["type"] == "photo":
                kwargs = {
                    "chat_id": target_chat_id,
                    "photo": item["url"],
                    "caption": caption,
                    "parse_mode": "HTML"
                }
                if target_thread_id is not None:
                    kwargs["message_thread_id"] = target_thread_id
                if reply_to_tg_id:
                    kwargs["reply_to_message_id"] = reply_to_tg_id

                await state.tg_queue.put({
                    "method": "send_photo",
                    "kwargs": kwargs,
                    "max_chat_id": message.chat_id,
                    "max_message_id": message.id,
                    "body_text": (forward_text or message.text or "") or None,
                    "max_sender_id": message.sender,
                    "db_path": db_path,
                })
            else:
                kwargs = {
                    "chat_id": target_chat_id,
                    "video": item["url"],
                    "caption": (prefix + fwd_line).strip() or None,
                    "parse_mode": "HTML" if (prefix or fwd_line) else None,
                }
                if target_thread_id is not None:
                    kwargs["message_thread_id"] = target_thread_id
                if reply_to_tg_id:
                    kwargs["reply_to_message_id"] = reply_to_tg_id

                body_follow = maybe_spoiler(await apply_pings(forward_text or ""))
                task = {
                    "method": "send_video",
                    "kwargs": kwargs,
                    "max_chat_id": message.chat_id,
                    "max_message_id": message.id,
                    "body_text": forward_text or None,
                    "max_sender_id": message.sender,
                    "followup_text": body_follow or None,
                    "db_path": db_path,
                }
                # Размер этого видео не был подтверждён — если полная отправка
                # не удастся именно из-за размера/скачивания, tg_worker сам
                # откатится на превью вместо того чтобы просто показать ошибку.
                if item.get("fallback_thumbnail"):
                    fallback_kwargs = {
                        "chat_id": target_chat_id,
                        "photo": item["fallback_thumbnail"],
                        # Точную причину допишет tg_worker перед отправкой —
                        # см. комментарий в ветке альбома выше.
                        "caption": (
                            (prefix + fwd_line).strip()
                            + ("\n" if (prefix or fwd_line) else "")
                        ),
                        "parse_mode": "HTML",
                    }
                    if target_thread_id is not None:
                        fallback_kwargs["message_thread_id"] = target_thread_id
                    if reply_to_tg_id:
                        fallback_kwargs["reply_to_message_id"] = reply_to_tg_id
                    task["fallback"] = {"method": "send_photo", "kwargs": fallback_kwargs}
                await state.tg_queue.put(task)

        # Отправляем всё остальное
        for task in other_tasks:
            await state.tg_queue.put(task)

    else:
        # Просто текст
        body = maybe_spoiler(await apply_pings(forward_text or ""))
        if is_forward and body:
            text_to_send = fwd_line + body
        elif is_forward and not body:
            text_to_send = fwd_line.strip() + " (без текста)"
        else:
            text_to_send = body

        if text_to_send:
            kwargs = {
                "chat_id": target_chat_id,
                "text": prefix + text_to_send,
                "parse_mode": "HTML"
            }
            if target_thread_id is not None:
                kwargs["message_thread_id"] = target_thread_id
            if reply_to_tg_id:
                kwargs["reply_to_message_id"] = reply_to_tg_id

            await state.tg_queue.put({
                "method": "send_message",
                "kwargs": kwargs,
                "max_chat_id": message.chat_id,
                "max_message_id": message.id,
                "body_text": (forward_text or message.text or "") or None,
                "max_sender_id": message.sender,
                "db_path": db_path,
            })


async def _handle_message_edit(message: MaxMessage, client: Client, *, db_path: str):
    try:
        mapping = await get_tg_message_id(message.chat_id, message.id, db_path=db_path)
        if not mapping:
            state.log.warning(f"Нет маппинга для отредактированного сообщения max_id={message.id}")
            return

        tg_chat_id, tg_thread_id, tg_message_id = mapping
        new_text = message.text or ""

        if not new_text:
            return

        # Сохраняем префикс имени, если это группа
        prefix = ""
        if message.chat_id < 0 and message.sender is not None:
            try:
                name = await format_sender_name(client, message.sender, db_path=db_path)
                prefix = f"<b>{safe_html(name)}</b>\n"
            except Exception:
                pass

        await state.tg_bot.edit_message_text(
            chat_id=tg_chat_id,
            message_id=tg_message_id,
            text=prefix + maybe_spoiler(await _apply_pings_base(new_text, db_path=db_path)),
            parse_mode="HTML"
        )
        state.log.info(f"MAX → TG: сообщение отредактировано (tg_id={tg_message_id})")

    except Exception as e:
        state.log.exception(f"Ошибка редактирования MAX → TG: {e}")


async def _handle_message_delete(event, client: Client, *, db_path: str):
    try:
        max_chat_id = getattr(event, "chat_id", None)
        max_message_id = (
            getattr(event, "id", None)
            or getattr(event, "message_id", None)
            or getattr(event, "message_ids", None)
        )

        if isinstance(max_message_id, (list, tuple)):
            ids = list(max_message_id)
        else:
            ids = [max_message_id] if max_message_id is not None else []

        if not max_chat_id or not ids:
            state.log.warning(f"Удаление MAX: неполные данные event={event!r}")
            return

        mark = "<blockquote><i>🗑 Сообщение удалено в MAX</i></blockquote>"

        for mid in ids:
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    """
                    SELECT tg_chat_id, tg_thread_id, tg_message_id, body_text, max_sender_id
                    FROM messages
                    WHERE max_chat_id = ? AND max_message_id = ?
                    """,
                    (max_chat_id, mid),
                ) as cur:
                    row = await cur.fetchone()

            if not row:
                state.log.info(f"Удаление MAX: нет маппинга max_id={mid}")
                continue

            tg_chat_id, tg_thread_id, tg_message_id, body_text, max_sender_id = row
            body = (body_text or "").strip()

            prefix = ""
            if max_sender_id and max_chat_id is not None and max_chat_id < 0:
                try:
                    name = await format_sender_name(client, int(max_sender_id), db_path=db_path)
                    prefix = f"<b>{safe_html(name)}</b>\n"
                except Exception:
                    pass

            if body:
                new_text = f"{prefix}{safe_html(body)}\n\n{mark}"
            else:
                new_text = f"{prefix}{mark}" if prefix else mark

            if len(new_text) > 1024:
                keep = 1024 - len(mark) - len(prefix) - 5
                if keep < 50:
                    new_text = f"{prefix}{mark}" if prefix else mark
                else:
                    new_text = f"{prefix}{safe_html(body[:keep])}…\n\n{mark}"

            try:
                await state.tg_bot.edit_message_text(
                    chat_id=tg_chat_id,
                    message_id=tg_message_id,
                    text=new_text,
                    parse_mode="HTML",
                )
                state.log.info(f"MAX → TG: пометка удаления (text) tg_id={tg_message_id}")
                continue
            except Exception:
                pass

            try:
                await state.tg_bot.edit_message_caption(
                    chat_id=tg_chat_id,
                    message_id=tg_message_id,
                    caption=new_text if len(new_text) <= 1024 else mark,
                    parse_mode="HTML",
                )
                state.log.info(f"MAX → TG: пометка удаления (caption) tg_id={tg_message_id}")
                continue
            except Exception as e:
                state.log.warning(f"Не удалось отметить удаление tg_id={tg_message_id}: {e}")

    except Exception as e:
        state.log.exception(f"Ошибка обработки удаления MAX → TG: {e}")


async def _mirror_reaction_to_tg(tg_chat_id: int, tg_message_id: int, reaction_emoji: str | None):
    """Общее для push- и poll-пути: одна реакция MAX (наибольшая по счёту,
    если их несколько) -> одна реакция бота в Telegram. Telegram и MAX не
    совпадают по написанию одних и тех же эмодзи (MAX хранит с variation
    selector U+FE0F — "❤️", у Telegram в каталоге разрешённых реакций та же
    эмодзи без него — "❤", подтверждено через get_reactions) — один ретрай
    без селектора, если он был в строке."""
    reaction_list = [ReactionTypeEmoji(emoji=reaction_emoji)] if reaction_emoji else []
    try:
        await state.tg_bot.set_message_reaction(
            chat_id=tg_chat_id,
            message_id=tg_message_id,
            reaction=reaction_list,
        )
        return
    except Exception as e:
        last_error = e

    if reaction_emoji and "️" in reaction_emoji:
        try:
            await state.tg_bot.set_message_reaction(
                chat_id=tg_chat_id,
                message_id=tg_message_id,
                reaction=[ReactionTypeEmoji(emoji=reaction_emoji.replace("️", ""))],
            )
            return
        except Exception as e:
            last_error = e

    # У MAX реакций заметно больше, чем в фиксированном каталоге Telegram
    # (например "💀" там просто нет) — вместо того чтобы совсем ничего не
    # показать, ставим нейтральное ❤️ как универсальный fallback. Не делаем
    # этого, когда реакцию как раз СНИМАЮТ (reaction_emoji is None) — там
    # пустой список и так должен проходить всегда.
    if reaction_emoji:
        try:
            await state.tg_bot.set_message_reaction(
                chat_id=tg_chat_id,
                message_id=tg_message_id,
                reaction=[ReactionTypeEmoji(emoji="❤")],
            )
            return
        except Exception as e:
            last_error = e

    # Не критично: сообщение уже недоступно для реакций или сама попытка
    # сломалась иначе — тихо логируем, без тревоги в топике (реакции —
    # некритичная фича).
    state.log.warning(f"Не удалось отразить реакцию MAX → TG (tg_id={tg_message_id}): {last_error}")


async def _handle_reaction_update(event, client: Client, *, db_path: str):
    """MAX присылает не дельту (кто именно поставил/снял реакцию), а полный
    текущий срез счётчиков по каждому эмодзи — как и в комментарии к
    ReactionUpdateEvent в самой pymax, отправителя реакции узнать нельзя.
    На практике это событие ещё и не приходит вовсе для реакций, поставленных
    в личных диалогах (get_reactions видит реакцию, это событие — никогда) —
    оставлен на случай, если для групп или в
    будущей версии MAX push всё же сработает; основной рабочий путь —
    периодический опрос ниже (_reaction_poll_loop)."""
    try:
        max_chat_id = getattr(event, "chat_id", None)
        max_message_id = getattr(event, "message_id", None)
        if max_chat_id is None or max_message_id is None:
            return
        max_message_id = int(max_message_id)

        mapping = await get_tg_message_id(max_chat_id, max_message_id, db_path=db_path)
        if not mapping:
            return
        tg_chat_id, tg_thread_id, tg_message_id = mapping

        counters = {c.reaction: c.count for c in (event.counters or []) if c.count > 0}
        reaction_emoji = max(counters, key=counters.get) if counters else None

        await set_known_reaction(max_chat_id, max_message_id, reaction_emoji, db_path=db_path)
        await _mirror_reaction_to_tg(tg_chat_id, tg_message_id, reaction_emoji)
    except Exception as e:
        state.log.exception(f"Ошибка обработки реакции MAX → TG: {e}")


_REACTION_POLL_INTERVAL = 30  # секунд между опросами — get_reactions один батч-вызов на чат, не на сообщение, так что это дёшево
_REACTION_POLL_WINDOW_HOURS = 2  # опрашиваем только сообщения младше этого


async def _poll_reactions_once(client: Client, *, db_path: str):
    rows = await get_recent_messages_for_reaction_poll(_REACTION_POLL_WINDOW_HOURS, db_path=db_path)
    if not rows:
        return

    by_chat: dict[int, list[tuple]] = {}
    for row in rows:
        by_chat.setdefault(row[0], []).append(row)

    for max_chat_id, chat_rows in by_chat.items():
        message_ids = [row[1] for row in chat_rows]
        try:
            result = await client.get_reactions(chat_id=max_chat_id, message_ids=message_ids)
        except Exception as e:
            state.log.warning(f"Опрос реакций: get_reactions упал chat_id={max_chat_id}: {e}")
            continue
        if not result:
            continue

        for _, max_message_id, tg_chat_id, tg_thread_id, tg_message_id in chat_rows:
            info = result.get(str(max_message_id)) or result.get(max_message_id)
            counters = {c.reaction: c.count for c in (info.counters if info else []) if c.count > 0}
            current = max(counters, key=counters.get) if counters else None

            known = await get_known_reaction(max_chat_id, max_message_id, db_path=db_path)
            if current == known:
                continue

            await set_known_reaction(max_chat_id, max_message_id, current, db_path=db_path)
            await _mirror_reaction_to_tg(tg_chat_id, tg_message_id, current)


async def _reaction_poll_loop(client: Client, *, db_path: str):
    """MAX не шлёт push о новых реакциях в личных диалогах (см. докстринг
    _handle_reaction_update) — единственный рабочий способ узнать про
    MAX-реакцию это спросить сервер самим. Опрашиваем только недавние
    сообщения (_REACTION_POLL_WINDOW_HOURS), не все подряд."""
    while True:
        await asyncio.sleep(_REACTION_POLL_INTERVAL)
        try:
            await _poll_reactions_once(client, db_path=db_path)
        except Exception as e:
            state.log.warning(f"Опрос реакций упал: {e}")


def _msg_time_seconds(msg) -> int:
    """message.time не документирован однозначно (сек. или мс. Unix time) —
    нормализуем по порядку величины: миллисекунды для дат после ~2001 года
    всегда > 10**12, секунды — нет."""
    t = int(getattr(msg, "time", 0) or 0)
    return t // 1000 if t > 10**12 else t


async def _backfill_missed_messages(client: Client, *, tg_group_id: int, db_path: str, backward: int = 30):
    """При каждом логине (включая реконнект по сохранённой сессии) явно
    запрашиваем историю (chat.history()) по каждому уже известному диалогу
    этой TG-группы — пассивный client.messages из логин-ответа на практике
    оказывается пустым при реконнекте.

    ВАЖНО: history() отдаёт реальную историю переписки, а не только то, что
    накопилось за простой — без фильтра по времени это рассылает НЕДЕЛИ
    старой переписки дубликатами (отсутствие сообщения в таблице messages
    не значит, что его удалила чистка — оно может быть просто старше самого
    моста и никогда не отслеживалось). Поэтому берём
    только сообщения новее last_seen_at (обновляется heartbeat'ом, пока
    клиент подключён, см. register_tenant_handlers) — а проверка по
    messages остаётся вторым, а не единственным барьером."""
    try:
        profile_id = client.me.contact.id
    except Exception:
        profile_id = "?"

    now = int(time.time())
    cutoff = await get_last_seen_at(db_path=db_path)
    if cutoff is None:
        # Первый раз видим этот аккаунт с этой фичей — не знаем, с какого
        # момента реально считать "пропущенным", поэтому не гадаем:
        # запоминаем текущее время и в этот раз ничего не досылаем.
        await set_last_seen_at(now, db_path=db_path)
        state.log.info(f"Добор пропущенного profile={profile_id}: нет сохранённой метки, пропускаю (запомнил {now})")
        return

    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT max_chat_id FROM dialogs WHERE telegram_chat_id = ?",
                (tg_group_id,),
            ) as cursor:
                chat_ids = [row[0] for row in await cursor.fetchall()]
    except Exception as e:
        state.log.warning(f"Добор пропущенного profile={profile_id}: не удалось получить список диалогов: {e}")
        return

    state.log.info(
        f"Добор пропущенного profile={profile_id}: проверяю {len(chat_ids)} диалог(ов), "
        f"граница — {now - cutoff}с назад"
    )

    for chat_id in chat_ids:
        try:
            chat = await client.get_chat(chat_id)
            if chat is None:
                continue
            history = await chat.history(backward=backward)
        except Exception as e:
            state.log.warning(f"Добор пропущенного profile={profile_id}: history chat_id={chat_id} упал: {e}")
            continue

        recent = [m for m in (history or []) if _msg_time_seconds(m) >= cutoff]
        if not recent:
            continue
        state.log.info(
            f"Добор пропущенного profile={profile_id}: chat_id={chat_id} — "
            f"{len(recent)} сообщени(й) новее границы (из {len(history)} в истории)"
        )

        for msg in sorted(recent, key=lambda m: getattr(m, "time", 0) or 0):
            try:
                if msg.chat_id is None:
                    msg.chat_id = chat_id
                already = await get_tg_message_id(chat_id, msg.id, db_path=db_path)
                if already:
                    continue
                state.log.info(f"Добираю пропущенное MAX-сообщение chat_id={chat_id} msg_id={msg.id}")
                await _handle_max_message(msg, client, tg_group_id=tg_group_id, db_path=db_path)
            except Exception as e:
                state.log.warning(
                    f"Не удалось дослать пропущенное сообщение chat_id={chat_id} "
                    f"msg_id={getattr(msg, 'id', '?')}: {e}"
                )

    await set_last_seen_at(now, db_path=db_path)


def register_tenant_handlers(client: Client, *, tg_group_id: int, db_path: str):
    """Навешивает MAX-обработчики на произвольный Client — используется и
    для твоего основного клиента (см. низ файла), и для клиентов tenant'ов
    (вызывается из tenants.py после успешного /setupgroup)."""

    async def _heartbeat():
        """Раз в 3 минуты, пока клиент подключён, отмечаем 'точно на связи' —
        так граница для добора пропущенных (_backfill_missed_messages)
        остаётся свежей даже без входящих сообщений, а не тянется от
        случайного прошлого момента."""
        while True:
            try:
                await set_last_seen_at(int(time.time()), db_path=db_path)
            except Exception as e:
                state.log.warning(f"heartbeat last_seen_at: {e}")
            await asyncio.sleep(180)

    @client.on_start()
    async def _on_start(c):
        print("MAX запущен")
        print(c.me)
        await asyncio.sleep(5)
        print("Готов принимать сообщения")
        await _backfill_missed_messages(c, tg_group_id=tg_group_id, db_path=db_path)
        if not getattr(c, "_backfill_heartbeat_started", False):
            c._backfill_heartbeat_started = True
            asyncio.create_task(_heartbeat())
        if not getattr(c, "_reaction_poll_started", False):
            c._reaction_poll_started = True
            asyncio.create_task(_reaction_poll_loop(c, db_path=db_path))

    @client.on_message()
    async def _on_message(message: MaxMessage, c: Client):
        # Диагностическое логирование: одно и то же сообщение в общей
        # MAX-группе иногда долетает только до ОДНОГО из двух
        # аккаунтов-участников, никогда до обоих сразу —
        # тег profile= позволит увидеть в journalctl, какой именно клиент
        # реально получил конкретное событие on_message.
        try:
            profile_id = c.me.contact.id
        except Exception:
            profile_id = "?"
        state.log.info(
            f"MAX on_message profile={profile_id} max_chat_id={message.chat_id} "
            f"msg_id={getattr(message, 'id', None)} sender={message.sender} -> tg_group={tg_group_id}"
        )
        await _handle_max_message(message, c, tg_group_id=tg_group_id, db_path=db_path)

    @client.on_message_edit()
    async def _on_message_edit(message: MaxMessage, c: Client):
        await _handle_message_edit(message, c, db_path=db_path)

    @client.on_message_delete()
    async def _on_message_delete(event, c: Client):
        await _handle_message_delete(event, c, db_path=db_path)

    @client.on_reaction_update()
    async def _on_reaction_update(event, c: Client):
        await _handle_reaction_update(event, c, db_path=db_path)


# Твоя собственная регистрация — тот же register_tenant_handlers(), с теми
# же параметрами (TG_GROUP_ID из конфига, state.DB_PATH), что были раньше
# зашиты напрямую в декораторы. Поведение не меняется.
register_tenant_handlers(state.max_client, tg_group_id=TG_GROUP_ID, db_path=state.DB_PATH)
