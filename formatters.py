import html
import re

SPOILER_LIMIT = 400

def safe_html(text: str) -> str:
    if not text:
        return ""
    return html.escape(str(text))

def maybe_spoiler(text: str, limit: int = SPOILER_LIMIT) -> str:
    """text уже после safe_html."""
    if not text:
        return ""
    if len(text) > limit:
        return f"<blockquote expandable>{text}</blockquote>"
    return text

def fit_caption(text: str | None, limit: int = 1024) -> str | None:
    if not text:
        return None
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"

def unwrap_forward(message):
    """
    Возвращает:
      is_forward, src_msg, src_chat_id, src_text, src_attaches, origin_label
    origin_label — имя автора/чата исходного сообщения (или "").
    """
    link = getattr(message, "link", None)
    if link is None:
        return False, message, getattr(message, "chat_id", None), (message.text or ""), (message.attaches or []), ""

    ltype = getattr(link, "type", None)
    ltype = ltype.value if hasattr(ltype, "value") else ltype
    if str(ltype).upper() != "FORWARD":
        return False, message, getattr(message, "chat_id", None), (message.text or ""), (message.attaches or []), ""

    src = getattr(link, "message", None)
    src_chat = getattr(link, "chat_id", None) or getattr(message, "chat_id", None)
    src_text = (getattr(src, "text", None) if src else None) or ""
    src_attaches = (getattr(src, "attaches", None) if src else None) or []

    # подпись «от кого»
    origin = (
        getattr(link, "chat_name", None)
        or ""
    )
    if not origin and src is not None:
        sender = getattr(src, "sender", None)
        if sender is not None:
            origin = str(sender)

    comment = (message.text or "").strip()  # комментарий к пересылке, если был
    text = src_text
    if comment and comment != src_text:
        text = f"{comment}\n{src_text}".strip() if src_text else comment

    attaches = list(src_attaches or [])
    seen = set(id(a) for a in attaches)
    for a in (message.attaches or []):
        if id(a) not in seen:
            attaches.append(a)
            seen.add(id(a))
    if not attaches:
        attaches = list(message.attaches or [])
    return True, src or message, src_chat, text, attaches, origin