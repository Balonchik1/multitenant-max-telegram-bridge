import pytest

import relay_max_to_tg as r


class DeniedError(Exception):
    def __init__(self, tag="first"):
        super().__init__(f"Нет прав на доступ к файлу Key: error.user.file.access [{tag}]")


class FakeClient:
    def __init__(self, allowed):
        self.allowed = allowed
        self.calls = []

    async def get_file_by_id(self, *, chat_id, message_id, file_id):
        self.calls.append((chat_id, message_id))
        if (chat_id, message_id) in self.allowed:
            return {"url": "http://example/file"}
        raise DeniedError(tag=f"{chat_id}/{message_id}")


async def _fetch(client, **overrides):
    args = dict(chat_id=1, message_id=10, file_id=5, alt_chat_id=2, alt_message_id=20)
    args.update(overrides)
    return await r._get_file_info_with_fallback(client, **args)


async def test_forward_falls_back_to_forwarded_message_when_original_denied():
    client = FakeClient(allowed={(2, 20)})
    info = await _fetch(client)

    assert info == {"url": "http://example/file"}
    assert client.calls == [(1, 10), (2, 20)]


async def test_direct_success_does_not_retry():
    client = FakeClient(allowed={(1, 10)})
    await _fetch(client)

    assert client.calls == [(1, 10)]


async def test_both_denied_raises_the_first_error():
    client = FakeClient(allowed=set())
    with pytest.raises(DeniedError) as exc:
        await _fetch(client)

    assert "1/10" in str(exc.value)
    assert client.calls == [(1, 10), (2, 20)]


async def test_non_access_error_is_not_retried():
    class Broken:
        calls = 0

        async def get_file_by_id(self, **kwargs):
            Broken.calls += 1
            raise RuntimeError("обрыв соединения")

    with pytest.raises(RuntimeError):
        await _fetch(Broken())
    assert Broken.calls == 1


async def test_same_ids_are_not_retried():
    client = FakeClient(allowed=set())
    with pytest.raises(DeniedError):
        await _fetch(client, alt_chat_id=1, alt_message_id=10)

    assert client.calls == [(1, 10)]


def test_access_denied_detection():
    assert r._is_file_access_denied(DeniedError())
    assert not r._is_file_access_denied(RuntimeError("HTTP 500"))
