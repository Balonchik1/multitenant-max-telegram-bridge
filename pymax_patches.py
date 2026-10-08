"""Заплатки поверх pymax (версия закреплена в requirements.txt).

upload_photo: pymax достаёт id фото из параметра photoIds адреса загрузки,
который отдаёт MAX, и падает с «Photo upload URL does not contain photoIds»,
если параметра нет. Здесь тот же порядок действий, но id берётся из самого
ответа MAX после загрузки, когда его нет в адресе. Если фото в ответе одно —
токен берём у него; если несколько и ни одно не подходит — честная ошибка.
"""
import asyncio
from http import HTTPStatus
from urllib.parse import parse_qs, quote, urlparse

import aiohttp
from pydantic import ValidationError
from pymax.api.response import payload_item
from pymax.api.uploads import service as _upload_service
from pymax.api.uploads.models import PhotoUploadResponse
from pymax.api.uploads.payloads import AttachPhotoPayload, UploadPayload
from pymax.exceptions import UploadError
from pymax.protocol import Opcode

import state


def pick_photo_token(photos: dict, photo_id: str | None):
    """Токен фото из ответа MAX: по id из адреса, а если его нет или он не
    совпал — у единственного фото в ответе."""
    if photo_id is not None and photo_id in photos:
        return photos[photo_id].token
    if len(photos) == 1:
        return next(iter(photos.values())).token
    raise KeyError(photo_id)


async def _upload_photo(self, photo, profile: bool = False) -> AttachPhotoPayload:
    state.log.info("Uploading photo")

    try:
        data = await self.app.invoke(
            Opcode.PHOTO_UPLOAD,
            payload=UploadPayload(profile=profile).to_payload(),
        )
    except Exception as e:
        raise UploadError("Failed to request photo upload URL") from e

    try:
        url = payload_item(data, "url", str)
    except Exception as e:
        raise UploadError("Failed to parse photo upload URL from response") from e
    if not url:
        raise UploadError("No upload URL received")

    parsed = urlparse(url)
    photo_ids = parse_qs(parsed.query).get("photoIds")
    photo_id = str(photo_ids[0]) if photo_ids else None
    if photo_id is None:
        # Сам адрес не пишем: в нём одноразовая подпись загрузки.
        state.log.warning(
            f"Адрес загрузки фото без photoIds: host={parsed.netloc} path={parsed.path} "
            f"query_keys={sorted(parse_qs(parsed.query))}"
        )

    try:
        photo_data = photo.validate_photo()
    except Exception as e:
        raise UploadError("Photo validation crashed") from e
    if not photo_data:
        raise UploadError("Photo validation failed")

    try:
        photo_bytes = await photo.read()
    except Exception as e:
        raise UploadError("Failed to read photo bytes") from e

    form = aiohttp.FormData()
    form.add_field(
        name="file",
        value=photo_bytes,
        filename=f"image.{quote(photo_data[0])}",
        content_type=photo_data[1],
    )

    try:
        async with (
            aiohttp.ClientSession(proxy=self.app.config.proxy) as session,
            session.post(url=url, data=form) as response,
        ):
            if response.status != HTTPStatus.OK:
                raise UploadError(f"Photo upload failed with status {response.status}")
            try:
                result = await response.json()
            except Exception as e:
                raise UploadError("Failed to decode photo upload response JSON") from e
    except UploadError:
        raise
    except aiohttp.ClientError as e:
        raise UploadError("HTTP error during photo upload") from e
    except asyncio.TimeoutError as e:
        raise UploadError("Timed out during photo upload") from e
    except Exception as e:
        raise UploadError("Unexpected error during photo upload") from e

    try:
        model = PhotoUploadResponse.model_validate(result)
    except ValidationError as e:
        raise UploadError("Invalid photo upload response model") from e

    try:
        token = pick_photo_token(model.photos, photo_id)
    except KeyError as e:
        raise UploadError(
            f"Photo upload response does not contain token for photo_id={photo_id} "
            f"(photos in response: {sorted(model.photos)})"
        ) from e

    if photo_id is None:
        state.log.info("Фото загружено, id взят из ответа MAX")
    return AttachPhotoPayload(photo_token=token)


_upload_service.UploadService.upload_photo = _upload_photo
