import asyncio

import pytest
from pymax import ApiError
from pymax.exceptions import UploadError

import relay_tg_to_max as r


class FakeVoice:
    def __init__(self, path, duration):
        self.kind = "voice"


class FakeFile:
    def __init__(self, path, name):
        self.kind = "file"


@pytest.fixture(autouse=True)
def fake_attachments(monkeypatch):
    monkeypatch.setattr(r, "Voice", FakeVoice)
    monkeypatch.setattr(r, "File", FakeFile)


class FakeClient:
    def __init__(self, voice_behavior):
        self.voice_behavior = voice_behavior
        self.sent_kinds = []

    async def send_message(self, *, chat_id, text, reply_to, attachments):
        kind = attachments[0].kind
        self.sent_kinds.append(kind)
        if kind == "voice":
            return await self.voice_behavior()
        return "SENT-AS-FILE"


async def _send(client):
    return await r._send_voice_with_fallback(
        client, chat_id=1, text=None, reply_to=None, path="/tmp/x.ogg", duration_ms=3000
    )


async def test_voice_goes_as_voice_when_max_accepts_it():
    async def ok():
        return "SENT-AS-VOICE"

    client = FakeClient(ok)
    assert await _send(client) == "SENT-AS-VOICE"
    assert client.sent_kinds == ["voice"]


async def test_not_ready_error_falls_back_to_file():
    async def not_ready():
        raise ApiError(opcode=64, error="attachment.not.ready")

    client = FakeClient(not_ready)
    assert await _send(client) == "SENT-AS-FILE"
    assert client.sent_kinds == ["voice", "file"]


async def test_upload_error_falls_back_to_file():
    async def broken():
        raise UploadError("Timed out waiting for video processing video_id=1")

    client = FakeClient(broken)
    assert await _send(client) == "SENT-AS-FILE"


async def test_waiting_too_long_falls_back_to_file(monkeypatch):
    monkeypatch.setattr(r, "_VOICE_READY_TIMEOUT", 0.05)

    async def hangs():
        await asyncio.sleep(5)

    client = FakeClient(hangs)
    assert await asyncio.wait_for(_send(client), timeout=2) == "SENT-AS-FILE"


async def test_other_api_errors_are_not_swallowed():
    async def forbidden():
        raise ApiError(opcode=64, error="chat.access.denied")

    client = FakeClient(forbidden)
    with pytest.raises(ApiError):
        await _send(client)
    assert client.sent_kinds == ["voice"]
