"""
Изолированная проверка логики asyncio.wait(..., FIRST_COMPLETED) +
определения unexpected_exit в bridge.py main() — без реального запуска
Telegram/MAX клиентов (это было бы integration-тестом на весь бот).
"""
import asyncio

_GRACE = 0.2  # то же окно, что и в bridge.py


async def _simulate(stop_delay: float | None, task_raises: bool) -> bool:
    """stop_delay=None -> сигнала остановки не было вообще (реальное падение).
    stop_delay=X -> stop_event.set() происходит через X секунд ПОСЛЕ того,
    как другая задача уже завершилась (эмулирует гонку: aiogram's
    dp.start_polling() реагирует на SIGTERM и сам завершает polling_task
    раньше, чем event loop успевает вызвать наш обработчик сигнала)."""
    stop_event = asyncio.Event()

    async def clean_task():
        await stop_event.wait()

    async def maybe_failing_task():
        if task_raises:
            raise RuntimeError("chats.outage")
        return "finished cleanly (unexpectedly)"

    polling_task = asyncio.create_task(clean_task())
    max_task = asyncio.create_task(maybe_failing_task())
    stopper = asyncio.create_task(stop_event.wait())

    if stop_delay is not None:
        asyncio.get_event_loop().call_later(stop_delay, stop_event.set)

    await asyncio.wait({polling_task, max_task, stopper}, return_when=asyncio.FIRST_COMPLETED)

    # см. bridge.py: короткая пауза, чтобы догнать обработчик сигнала,
    # если он ещё не успел выставить stop_event
    await asyncio.sleep(_GRACE)
    unexpected_exit = not stop_event.is_set()

    for t in (polling_task, max_task, stopper):
        t.cancel()
    await asyncio.sleep(0.05)

    return unexpected_exit


async def test_real_crash_with_no_signal_is_unexpected():
    assert await _simulate(stop_delay=None, task_raises=True) is True


async def test_task_finishes_cleanly_with_no_signal_is_still_unexpected():
    """Задача завершилась сама, без исключения, но и сигнала остановки не
    было вообще — не штатная ситуация, тоже требует перезапуска."""
    assert await _simulate(stop_delay=None, task_raises=False) is True


async def test_race_signal_lags_within_grace_window():
    """Живой случай: aiogram сам реагирует на SIGTERM и завершает
    polling_task, а наш обработчик сигнала (stop_event.set()) срабатывает
    чуть позже — но всё ещё в пределах паузы-догонки. Раньше это ловилось
    как "неожиданное падение" на самом обычном systemctl restart."""
    assert await _simulate(stop_delay=0.05, task_raises=False) is False


async def test_signal_too_slow_beyond_grace_window_is_still_unexpected():
    """Если stop_event не успевает выставиться даже за окно-догонку —
    это уже не гонка сигнала, а настоящее падение без остановки."""
    assert await _simulate(stop_delay=_GRACE + 1.0, task_raises=False) is True
