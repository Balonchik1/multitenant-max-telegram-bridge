import types

import pytest
from pymax.api.uploads import service as upload_service
from pymax.exceptions import UploadError

import pymax_patches as p


class Tok:
    def __init__(self, token):
        self.token = token


def test_pick_token_by_id_from_url():
    assert p.pick_photo_token({"a": Tok("A"), "b": Tok("B")}, "b") == "B"


def test_pick_token_falls_back_to_only_photo_when_id_missing():
    assert p.pick_photo_token({"zzz": Tok("T")}, None) == "T"
    assert p.pick_photo_token({"zzz": Tok("T")}, "other") == "T"


def test_pick_token_refuses_to_guess_between_several_photos():
    with pytest.raises(KeyError):
        p.pick_photo_token({"a": Tok("A"), "b": Tok("B")}, None)


def test_patch_is_installed():
    assert upload_service.UploadService.upload_photo is p._upload_photo


class FakeResponse:
    status = 200

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    body = {}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, data):
        return FakeResponse(FakeSession.body)


class FakePhoto:
    def validate_photo(self):
        return ("png", "image/png")

    async def read(self):
        return b"bytes"


def make_service(monkeypatch, url, response_body):
    async def invoke(opcode, payload=None):
        return types.SimpleNamespace(url=url)

    monkeypatch.setattr(p, "payload_item", lambda data, key, typ: data.url)
    monkeypatch.setattr(p.aiohttp, "ClientSession", FakeSession)
    FakeSession.body = response_body
    app = types.SimpleNamespace(invoke=invoke, config=types.SimpleNamespace(proxy=None))
    return types.SimpleNamespace(app=app)


async def test_upload_works_when_url_has_no_photo_ids(monkeypatch):
    svc = make_service(
        monkeypatch,
        "https://upload.example/photo?sig=abc",
        {"photos": {"photo-77": {"token": "TOKEN-1"}}},
    )
    payload = await p._upload_photo(svc, FakePhoto())
    assert payload.photo_token == "TOKEN-1"


async def test_upload_still_uses_photo_ids_from_url_when_present(monkeypatch):
    svc = make_service(
        monkeypatch,
        "https://upload.example/photo?photoIds=photo-2&sig=abc",
        {"photos": {"photo-1": {"token": "WRONG"}, "photo-2": {"token": "RIGHT"}}},
    )
    payload = await p._upload_photo(svc, FakePhoto())
    assert payload.photo_token == "RIGHT"


async def test_upload_reports_clear_error_when_response_is_ambiguous(monkeypatch):
    svc = make_service(
        monkeypatch,
        "https://upload.example/photo?sig=abc",
        {"photos": {"a": {"token": "A"}, "b": {"token": "B"}}},
    )
    with pytest.raises(UploadError) as exc:
        await p._upload_photo(svc, FakePhoto())
    assert "photos in response" in str(exc.value)
