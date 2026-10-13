import asyncio
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import relay_tg_to_max as r
import state


class FakeSent:
    class chat:
        id = 1

    message_id = 11
    message_thread_id = None


@pytest.fixture
async def video_server():
    big = b"x" * 3000

    async def ok(request):
        return web.Response(body=b"VIDEO-BYTES", content_type="video/mp4")

    async def forbidden(request):
        return web.Response(status=403)

    async def declared_big(request):
        return web.Response(body=big, content_type="video/mp4")

    async def streamed_big(request):
        resp = web.StreamResponse()
        resp.content_type = "video/mp4"
        await resp.prepare(request)
        for _ in range(4):
            await resp.write(b"y" * 1000)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/ok.mp4", ok)
    app.router.add_get("/forbidden.mp4", forbidden)
    app.router.add_get("/declared.mp4", declared_big)
    app.router.add_get("/streamed.mp4", streamed_big)
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()


def _url(server, path):
    return str(server.make_url(path))


def test_url_fetch_error_detection():
    assert r._is_url_fetch_error(Exception("Bad Request: failed to get HTTP URL content"))
    assert r._is_url_fetch_error(Exception("Bad Request: wrong type of the web page content"))
    assert not r._is_url_fetch_error(Exception("Bad Request: file is too big"))
    assert not r._is_url_fetch_error(Exception("Forbidden: bot was kicked"))


async def test_download_returns_file_with_content(video_server):
    path = await r._download_video_for_upload(_url(video_server, "/ok.mp4"), max_bytes=1000)
    try:
        assert path and path.endswith(".mp4")
        assert open(path, "rb").read() == b"VIDEO-BYTES"
    finally:
        os.remove(path)


async def test_download_refuses_when_declared_size_exceeds_limit(video_server):
    assert await r._download_video_for_upload(_url(video_server, "/declared.mp4"), max_bytes=1000) is None


async def test_download_stops_when_streamed_size_exceeds_limit_and_cleans_up(video_server, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert await r._download_video_for_upload(_url(video_server, "/streamed.mp4"), max_bytes=1500) is None
    assert list(tmp_path.iterdir()) == []


async def test_download_returns_none_on_http_error(video_server):
    assert await r._download_video_for_upload(_url(video_server, "/forbidden.mp4"), max_bytes=1000) is None


async def test_retry_by_upload_sends_file_and_removes_temp_copy(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"data")
    sent_kwargs = {}

    async def fake_download(url, max_bytes):
        return str(video)

    async def fake_send(method, kwargs):
        sent_kwargs.update(kwargs)
        sent_kwargs["method"] = method
        return FakeSent()

    monkeypatch.setattr(r, "_download_video_for_upload", fake_download)
    monkeypatch.setattr(r, "_send_tg_method", fake_send)

    result = await r._retry_video_by_upload({"chat_id": 1, "video": "https://x/v.mp4", "caption": "подпись"})

    assert isinstance(result, FakeSent)
    assert sent_kwargs["method"] == "send_video"
    assert sent_kwargs["caption"] == "подпись"
    assert not isinstance(sent_kwargs["video"], str)
    assert not video.exists()


async def test_retry_by_upload_returns_none_when_download_fails(monkeypatch):
    async def fake_download(url, max_bytes):
        return None

    monkeypatch.setattr(r, "_download_video_for_upload", fake_download)
    assert await r._retry_video_by_upload({"chat_id": 1, "video": "https://x/v.mp4"}) is None


async def test_retry_by_upload_returns_none_and_cleans_up_when_upload_fails(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"data")

    async def fake_download(url, max_bytes):
        return str(video)

    async def failing_send(method, kwargs):
        raise RuntimeError("Telegram отказал")

    monkeypatch.setattr(r, "_download_video_for_upload", fake_download)
    monkeypatch.setattr(r, "_send_tg_method", failing_send)

    assert await r._retry_video_by_upload({"chat_id": 1, "video": "https://x/v.mp4"}) is None
    assert not video.exists()


class QueueBot:
    """Telegram: ссылка на видео не скачивается, файл — принимается."""

    def __init__(self, accept_upload=True):
        self.accept_upload = accept_upload
        self.video_calls = []
        self.photo_calls = []

    async def send_video(self, **kwargs):
        self.video_calls.append(kwargs)
        if isinstance(kwargs["video"], str):
            raise Exception("Telegram server says - Bad Request: failed to get HTTP URL content")
        if not self.accept_upload:
            raise Exception("Telegram server says - Bad Request: something else")
        return FakeSent()

    async def send_photo(self, **kwargs):
        self.photo_calls.append(kwargs)
        return FakeSent()


async def _run_one_task(monkeypatch, bot, task, download_path):
    async def fake_download(url, max_bytes):
        return download_path

    monkeypatch.setattr(state, "tg_bot", bot)
    monkeypatch.setattr(r, "_download_video_for_upload", fake_download)
    state.tg_queue = asyncio.Queue()
    worker = asyncio.create_task(r.tg_worker())
    await state.tg_queue.put(task)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if state.tg_queue.empty() and (bot.video_calls or bot.photo_calls):
            await asyncio.sleep(0.05)
            break
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


def _video_task(with_fallback=False):
    task = {
        "method": "send_video",
        "kwargs": {"chat_id": 1, "video": "https://cdn.example/big.mp4", "caption": "c"},
        "max_chat_id": None,
        "max_message_id": None,
    }
    if with_fallback:
        task["fallback"] = {
            "method": "send_photo",
            "kwargs": {"chat_id": 1, "photo": "https://cdn.example/t.jpg", "caption": "превью"},
        }
    return task


async def test_worker_uploads_file_when_telegram_cannot_fetch_the_url(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"data")
    bot = QueueBot()

    await _run_one_task(monkeypatch, bot, _video_task(with_fallback=True), str(video))

    assert len(bot.video_calls) == 2, "одна неудачная попытка по ссылке и одна успешная загрузка файлом"
    assert isinstance(bot.video_calls[0]["video"], str)
    assert not isinstance(bot.video_calls[1]["video"], str)
    assert bot.photo_calls == [], "до превью не дошло: видео отправилось целиком"


async def test_worker_falls_back_to_preview_when_download_is_impossible(monkeypatch):
    bot = QueueBot()

    await _run_one_task(monkeypatch, bot, _video_task(with_fallback=True), None)

    assert len(bot.photo_calls) == 1
    assert "только превью" in bot.photo_calls[0]["caption"]
