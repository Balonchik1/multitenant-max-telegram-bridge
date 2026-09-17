"""
Точка входа MAX <-> Telegram моста.

Модули регистрируют свои обработчики через декораторы (@state.dp.message,
@state.max_client.on_message и т.д.) в момент импорта — поэтому порядок
импортов ниже важен: relay_tg_to_max должен импортироваться ПОСЛЕДНИМ
среди модулей с @state.dp.message(...), так как его telegram_to_max —
catch-all без фильтра и обязан получать апдейты последним, уже после
того как более специфичные обработчики (auth_flow, commands) либо
обработали сообщение, либо явно передали его дальше через SkipHandler().
"""
import asyncio
import os
import signal

import state
import session_backup
from access import init_access_table
from config import SESSION_BACKUP_KEY
from db_messages import init_db, load_allowed_groups, cleanup_old_messages

import auth_flow  # noqa: F401  (регистрирует @state.dp.message, ловит SMS/2FA код)
import commands  # noqa: F401  (регистрирует /groups /link /ping /alias /chats ...)
import tenants  # noqa: F401  (самостоятельная регистрация: онбординг tenant'ов)
import relay_max_to_tg  # noqa: F401  (регистрирует @state.max_client.on_message и т.д.)
import relay_tg_to_max  # noqa: F401  (последним — содержит catch-all @state.dp.message())

log = state.log


async def main():
    state._main_loop = asyncio.get_running_loop()

    await init_db()
    await init_access_table(state.DB_PATH)
    await tenants.init_tenants_table()
    os.makedirs(state.TENANTS_ROOT, exist_ok=True)
    state.allowed_groups_runtime = await load_allowed_groups()
    log.info(f"Разрешённые группы: {state.allowed_groups_runtime}")

    await tenants.resume_all_tenants()

    await cleanup_old_messages(30)

    asyncio.create_task(relay_tg_to_max.tg_worker())

    async def periodic_cleanup():
        while True:
            await asyncio.sleep(24 * 60 * 60)
            try:
                await cleanup_old_messages(30)
            except Exception as e:
                log.error(f"Ошибка периодической очистки: {e}")

    asyncio.create_task(periodic_cleanup())

    def _run_session_backup():
        created = session_backup.backup_all_sessions(
            backups_root="session_backups",
            passphrase=SESSION_BACKUP_KEY,
            tenants_root=state.TENANTS_ROOT,
            on_error=lambda name, e: log.warning(f"Бэкап сессии {name} упал: {e}"),
        )
        if created:
            log.info(f"Бэкап сессий: создано {len(created)} файл(ов)")

    async def periodic_session_backup():
        # SESSION_BACKUP_KEY необязателен (см. config.py) — без него
        # автобэкапы просто отключены, это не ошибка конфигурации.
        if not SESSION_BACKUP_KEY:
            log.info("SESSION_BACKUP_KEY не задан — автобэкапы сессий отключены")
            return
        try:
            _run_session_backup()
        except Exception as e:
            log.error(f"Ошибка бэкапа сессий при старте: {e}")
        while True:
            await asyncio.sleep(24 * 60 * 60)
            try:
                _run_session_backup()
            except Exception as e:
                log.error(f"Ошибка периодического бэкапа сессий: {e}")

    asyncio.create_task(periodic_session_backup())

    stop_event = asyncio.Event()

    def _request_stop():
        log.warning("Получен сигнал остановки")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            state._main_loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: _request_stop())

    polling_task = asyncio.create_task(
        state.dp.start_polling(
            state.tg_bot,
            allowed_updates=["message", "edited_message", "callback_query", "message_reaction"],
            # aiogram по умолчанию сама ставит обработчик SIGINT/SIGTERM
            # (handle_signals=True) — он молча ПЕРЕЗАПИСЫВАЕТ наш
            # собственный (зарегистрирован выше), потому что второй
            # add_signal_handler для того же сигнала просто заменяет
            # первый. В итоге stop_event никогда не выставлялся на
            # реальном systemctl restart, и это всегда классифицировалось
            # как "неожиданное падение". Отключаем встроенный обработчик
            # aiogram — используем только свой.
            handle_signals=False,
        )
    )
    await asyncio.sleep(2)

    max_task = asyncio.create_task(state.max_client.start())

    # ждём либо сигнал, либо падение одной из задач
    stopper = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {polling_task, max_task, stopper},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # ВАЖНО: aiogram's dp.start_polling() тоже реагирует на SIGTERM и может
    # сам завершить polling_task чуть раньше, чем event loop успеет вызвать
    # наш собственный обработчик сигнала (_request_stop → stop_event.set())
    # — оба реагируют на один и тот же сигнал, но порядок между двумя
    # независимыми колбэками не гарантирован. Без паузы это иногда ловилось
    # как "неожиданное падение" на самом обычном systemctl restart. Даём
    # обработчику сигнала короткое окно, чтобы догнать.
    await asyncio.sleep(0.2)

    # Если stop_event так и не установился — задача завершилась НЕ из-за
    # сигнала остановки, а упала сама по себе (например MAX ответил
    # "chats.outage" прямо при старте). Раньше это тоже вело к чистому
    # os._exit(0), который для systemd выглядит как "штатно остановился
    # сам" — Restart=on-failure на код 0 не срабатывает, и бот оставался
    # лежать до ручного вмешательства.
    unexpected_exit = not stop_event.is_set()
    if unexpected_exit:
        for t in done:
            exc = t.exception() if not t.cancelled() else None
            if exc:
                log.error(f"Задача завершилась с ошибкой, нужен перезапуск: {exc}")
            else:
                log.error("Задача неожиданно завершилась без ошибки, нужен перезапуск")

    log.info("Остановка: снимаю задачи…")
    for t in (polling_task, max_task, stopper):
        t.cancel()
    await asyncio.sleep(0.5)

    # на всякий случай закрыть бота
    try:
        await state.tg_bot.session.close()
    except Exception:
        pass

    exit_code = 1 if unexpected_exit else 0

    # если pymax всё ещё держит процесс — выход через 3 сек
    def _force_exit():
        log.warning("Принудительный выход (process exit)")
        os._exit(exit_code)

    state._main_loop.call_later(3.0, _force_exit)
    await asyncio.sleep(3.5)
    os._exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
