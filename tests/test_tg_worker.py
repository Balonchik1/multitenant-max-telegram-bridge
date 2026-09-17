import time

from aiogram.exceptions import TelegramRetryAfter

import state
import relay_tg_to_max as r


class FakeMethod:
    chat_id = 123


class FakeBot:
    def __init__(self, fail_times, retry_after=0.1):
        self.fail_times = fail_times
        self.calls = 0
        self.retry_after = retry_after

    async def send_message(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TelegramRetryAfter(method=FakeMethod(), message="flood", retry_after=self.retry_after)
        return "OK"


async def test_send_tg_method_recovers_after_flood_wait(monkeypatch):
    """_send_tg_method должен сам пересыпать при TelegramRetryAfter (flood
    control у Telegram) и в итоге успеть отправить, не тратя обычный
    лимит ретраев tg_worker'а только на ожидание лимита Telegram."""
    fake_bot = FakeBot(fail_times=2, retry_after=0.1)
    monkeypatch.setattr(state, "tg_bot", fake_bot)

    t0 = time.monotonic()
    result = await r._send_tg_method("send_message", {"chat_id": 1, "text": "hi"})
    elapsed = time.monotonic() - t0

    assert result == "OK"
    assert fake_bot.calls == 3
    assert elapsed >= 0.25, f"должен был проспать ~2 паузы по 0.1с+буфер, а прошло {elapsed:.2f}с"


async def test_send_tg_method_gives_up_after_max_attempts(monkeypatch):
    """Если flood control не отпускает дольше разумного числа попыток —
    в итоге должно подняться исходное исключение, а не зависнуть навечно."""
    fake_bot = FakeBot(fail_times=10, retry_after=0.05)
    monkeypatch.setattr(state, "tg_bot", fake_bot)

    try:
        await r._send_tg_method("send_message", {"chat_id": 1, "text": "hi"})
        assert False, "должно было поднять TelegramRetryAfter"
    except TelegramRetryAfter:
        assert fake_bot.calls == 6, fake_bot.calls


def test_is_size_related_error_matches_only_file_too_big():
    """"file is too big" — однозначно про размер. Остальные варианты
    _is_content_send_error (битая ссылка, неправильный тип содержимого)
    тоже мешают отправить видео целиком, но не обязательно про размер —
    fallback-подпись не должна утверждать это без уверенности."""
    assert r._is_size_related_error(Exception("Bad Request: file is too big")) is True
    assert r._is_content_send_error(Exception("Bad Request: file is too big")) is True

    assert r._is_size_related_error(Exception("failed to get HTTP URL content")) is False
    assert r._is_content_send_error(Exception("failed to get HTTP URL content")) is True

    assert r._is_size_related_error(Exception("wrong file identifier")) is False
    assert r._is_content_send_error(Exception("wrong file identifier")) is True
