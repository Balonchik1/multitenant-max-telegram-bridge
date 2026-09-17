"""Резолвинг USER_MENTION из MAX в @username/ссылку Telegram."""
from config import ADMIN_ID
from formatters import safe_html
from db_meta import get_mention_link
import state


async def apply_max_mentions(
    text: str, elements, client, max_chat_id: int,
    db_path: str | None = None, notify_id: int | None = ADMIN_ID,
) -> str:
    """
    USER_MENTION → @tg если есть /link.
    Если привязки нет и notify_id задан — пишем ему в личку (только у тебя;
    у tenant'ов notify_id=None — фича под твой конкретный workflow, им
    уведомления не нужны). db_path — своя таблица привязок, а не общая твоя.
    """
    if not text or not elements:
        return text or ""

    # собираем mentions
    mentions = []
    for el in elements:
        if isinstance(el, dict):
            etype = el.get("type")
            entity_id = el.get("entityId") or el.get("entity_id")
            length = el.get("length") or 0
            start = el.get("from_")
            if start is None:
                start = el.get("from")
        else:
            etype = getattr(el, "type", None)
            if hasattr(etype, "value"):
                etype = etype.value
            entity_id = getattr(el, "entityId", None) or getattr(el, "entity_id", None)
            length = getattr(el, "length", None) or 0
            start = getattr(el, "from_", None)
            if start is None:
                start = getattr(el, "from", None)

        if str(etype).upper() != "USER_MENTION" or not entity_id:
            continue
        if start is None:
            start = 0
        mentions.append((int(start), int(length), int(entity_id)))

    if not mentions:
        return text

    # с конца, чтобы индексы не плыли
    mentions.sort(key=lambda x: x[0], reverse=True)
    result = text
    notified = set()

    for start, length, entity_id in mentions:
        end = start + length
        if start < 0 or end > len(result) or length <= 0:
            # fallback: если from_=None и mention на всё слово
            if length and length <= len(result) and start == 0:
                end = length
            else:
                continue

        link = await get_mention_link(entity_id, db_path=db_path)
        if link:
            uname = (link[0] or "").strip() if not isinstance(link, str) else link.strip()
            tg_id = None if isinstance(link, str) else (link[1] if len(link) > 1 else None)
            if uname.lower() == "ignore":
                continue
            chunk = safe_html(result[start:end])
            if tg_id:
                repl = f'<a href="tg://user?id={int(tg_id)}">{chunk}</a>'
            elif uname:
                repl = f"@{uname.lstrip('@')}"
            else:
                repl = chunk
            result = result[:start] + repl + result[end:]
            continue

        # Уведомление про непривязанное упоминание — только для тебя
        # (notify_id=None у tenant'ов): фича сделана под твой конкретный
        # workflow (flat-группа с коллегами без MAX), другим пользователям
        # она не нужна и не должна их дёргать.
        if notify_id is None:
            continue

        # нет привязки — один раз за сообщение на entity_id
        if entity_id in notified:
            continue
        notified.add(entity_id)

        name = str(entity_id)
        try:
            user = await client.get_user(entity_id)
            if user and user.names:
                n = user.names[0]
                name = f"{n.first_name or ''} {n.last_name or ''}".strip() or n.name or name
        except Exception:
            pass

        snippet = text[:200]
        try:
            await state.tg_bot.send_message(
                chat_id=notify_id,
                text=(
                    "🔔 <b>Mention в MAX без привязки к TG</b>\n\n"
                    f"Имя: <b>{safe_html(name)}</b>\n"
                    f"MAX id: <code>{entity_id}</code>\n"
                    f"Чат MAX: <code>{max_chat_id}</code>\n"
                    f"Текст: {safe_html(snippet)}\n\n"
                    f"Привязать:\n<code>/link {entity_id} @username</code>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            state.log.warning(f"Не удалось написать {notify_id} про mention: {e}")

    return result
