"""Скачивание файлов из Telegram и сборка альбомов (TG -> MAX)."""
import os
import tempfile

import aiosqlite
from pymax import Photo, File, Video

import state
from db_messages import get_max_message_id, save_message_mapping


async def download_tg_file(file_id: str, suffix: str = "") -> str:
    """Скачивает файл из Telegram во временный файл и возвращает путь."""
    file = await state.tg_bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
    await state.tg_bot.download_file(file.file_path, destination=tmp_path)
    return tmp_path


async def safe_remove(path: str | None):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            state.log.warning(f"Не удалось удалить временный файл {path}: {e}")


async def process_media_group(media_group_id: str):
    """Собирает все сообщения альбома и отправляет их в MAX одним сообщением.

    Раньше была жёстко привязана к админскому аккаунту
    (state.allowed_groups_runtime/state.DB_PATH/state.max_client напрямую) —
    альбомы TG → MAX у tenant'ов из-за этого не работали в принципе.
    Теперь маршрутизация через _resolve_route(), как и везде."""
    messages = state.media_group_buffer.pop(media_group_id, [])
    state.media_group_timers.pop(media_group_id, None)

    if not messages:
        return

    # Берём первое сообщение для получения chat/thread и caption
    first = messages[0]
    if not first.message_thread_id:
        return

    from relay_tg_to_max import _resolve_route  # отложенный импорт, см. докстринг relay_tg_to_max.py
    max_client, db_path = _resolve_route(first.chat.id)
    if max_client is None:
        return

    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                # Фильтр и по чату, и по теме — thread_id сам по себе уникален
                # только внутри одной TG-группы, а не глобально между твоей
                # и всеми tenant'скими (был риск перепутать чужой диалог).
                "SELECT max_chat_id FROM dialogs WHERE telegram_chat_id = ? AND telegram_thread_id = ?",
                (first.chat.id, first.message_thread_id)
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            state.log.warning(f"Нет связи для альбома thread_id={first.message_thread_id}")
            return

        max_chat_id = row[0]
        caption = first.caption or first.text or ""

        # Reply для альбома
        reply_to_max_id = None
        if first.reply_to_message:
            mapping = await get_max_message_id(
                first.chat.id,
                first.reply_to_message.message_id,
                db_path=db_path,
            )
            if mapping:
                reply_to_max_id = mapping[1]

        attachments = []
        tmp_paths = []
        skipped = []

        try:
            for msg in messages:
                if msg.photo:
                    photo = msg.photo[-1]
                    if (photo.file_size or 0) > state.TG_DOWNLOAD_LIMIT:
                        skipped.append("фото > 20 МБ")
                        continue
                    path = await download_tg_file(photo.file_id, suffix=".jpg")
                    tmp_paths.append(path)
                    attachments.append(Photo(path=path))

                elif msg.video:
                    if (msg.video.file_size or 0) > state.TG_DOWNLOAD_LIMIT:
                        skipped.append("видео > 20 МБ")
                        continue
                    path = await download_tg_file(msg.video.file_id, suffix=".mp4")
                    tmp_paths.append(path)
                    attachments.append(Video(path=path))

                elif msg.document:
                    # документы в альбомах редко, но на всякий случай
                    if (msg.document.file_size or 0) > state.TG_DOWNLOAD_LIMIT:
                        skipped.append(f"файл «{msg.document.file_name or '?'}» > 20 МБ")
                        continue
                    name = msg.document.file_name or "file"
                    ext = os.path.splitext(name)[1] or ""
                    path = await download_tg_file(msg.document.file_id, suffix=ext)
                    tmp_paths.append(path)
                    # name= явно — иначе MAX берёт имя из временного файла
                    # (случайные буквы/цифры).
                    attachments.append(File(path=path, name=name))

            if not attachments:
                state.log.warning("Альбом пустой после фильтрации")
                if skipped:
                    try:
                        await state.tg_bot.send_message(
                            chat_id=first.chat.id,
                            message_thread_id=first.message_thread_id,
                            text="⚠️ Ничего из альбома не отправлено в MAX (лимит скачивания Telegram — 20 МБ): "
                            + "; ".join(skipped),
                        )
                    except Exception:
                        pass
                return

            sent = await max_client.send_message(
                chat_id=max_chat_id,
                text=caption,
                reply_to=reply_to_max_id,
                attachments=attachments
            )

            if sent and getattr(sent, "id", None):
                # Сохраняем маппинг по первому сообщению альбома
                await save_message_mapping(
                    max_chat_id=max_chat_id,
                    max_message_id=sent.id,
                    tg_chat_id=first.chat.id,
                    tg_thread_id=first.message_thread_id,
                    tg_message_id=first.message_id,
                    db_path=db_path,
                )

            state.log.info(f"TG → MAX: альбом из {len(attachments)} файлов")

            if skipped:
                try:
                    await state.tg_bot.send_message(
                        chat_id=first.chat.id,
                        message_thread_id=first.message_thread_id,
                        text="⚠️ Часть альбома не отправлена в MAX (лимит скачивания Telegram — 20 МБ): "
                        + "; ".join(skipped),
                    )
                except Exception:
                    pass

        finally:
            for path in tmp_paths:
                await safe_remove(path)

    except Exception as e:
        state.log.exception(f"Ошибка обработки альбома {media_group_id}: {e}")
        try:
            await state.tg_bot.send_message(
                chat_id=first.chat.id,
                message_thread_id=first.message_thread_id,
                text=f"⚠️ Не удалось переслать альбом в MAX: {e}",
            )
        except Exception:
            pass
