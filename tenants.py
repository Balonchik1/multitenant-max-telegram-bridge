"""
Самостоятельная регистрация пользователей: человек, которому в access.py
нажали «Разрешить», проходит вход в СВОЙ MAX-аккаунт прямо в переписке
с этим же ботом (без создания отдельного systemd-инстанса).

Статусы tenant_accounts.status:
  awaiting_phone  -> ждём номер телефона
  awaiting_login  -> номер получен, пытаемся войти (SMS-код спрашиваем у него же)
  needs_2fa       -> MAX потребовал включить облачный пароль, ждём /retry
  awaiting_group  -> вход выполнен, ждём пока человек привяжет свою TG-группу
  active          -> группа привязана, сообщения уже маршрутизируются
  failed          -> вход не удался по другой причине, ждём /retry

  /retry разрешён из needs_2fa/failed/awaiting_group/active — последние два
  нужны, когда бот перезапустился и живой Client потерялся из памяти
  (сессия на диске сохранена, /retry просто переподключается без нового кода).

Каждый tenant получает собственный Client (pymax) и собственную БД
(tenants/<owner_id>/bridge.db) — полная изоляция данных от основного
моста и от других tenant'ов, без ALTER'ов существующих таблиц.
"""
import asyncio
import os
import time

import aiosqlite
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message as TgMessage,
)
from pymax import Client

from config import ADMIN_ID
from formatters import safe_html
import auth_flow
import db_meta
import state

# owner_id -> asyncio.Task, который держит client.start() (долгоживущий).
# Клиенты и маппинг группа->владелец живут в state.py (см. её докстринг) —
# чтобы relay_tg_to_max.py мог их читать без цикла tenants<->relay_*.
_run_tasks: dict[int, asyncio.Task] = {}
# owner_id -> пароль, введённый в этой попытке, ждущий ответа на "запомнить?"
# (не читаем auth_flow.last_entered_password повторно в колбэке — он может
# уже смениться, если тем временем стартовал чей-то ещё логин).
_pending_remember_password: dict[int, str] = {}

# owner_id -> unix-время последнего уведомления "мост сломан" — чтобы не
# слать одно и то же в группу/личку на каждое упавшее сообщение.
_last_broken_notice: dict[int, float] = {}
_BROKEN_NOTICE_COOLDOWN = 3600  # 1 час


async def notify_group_broken(owner_id: int, tg_group_id: int, reason: str):
    """Зовём отовсюду, где отправка/создание темы в группе tenant'а падает
    (тема удалена, темы выключили, бота разжаловали или выгнали) — шлём
    и в группу (вдруг ещё видно), и в личку (на случай если бота уже выгнали)."""
    now = time.time()
    if now - _last_broken_notice.get(owner_id, 0) < _BROKEN_NOTICE_COOLDOWN:
        return
    _last_broken_notice[owner_id] = now

    text = (
        f"⚠️ Мост с MAX перестал работать в твоей группе: {reason}\n\n"
        "Проверь:\n"
        "— бот всё ещё администратор группы\n"
        "— у бота включено право «Управление темами»\n"
        "— «Темы» в группе не отключили\n"
        "— бота не удалили из группы"
    )
    try:
        await state.tg_bot.send_message(tg_group_id, text)
    except Exception:
        pass
    try:
        await state.tg_bot.send_message(owner_id, text)
    except Exception:
        pass


async def resume_all_tenants():
    """Вызывается один раз при старте bridge.py — переподключает MAX-сессии
    всех tenant'ов, у кого уже был (или шёл) успешный вход, без нового
    кода/пароля (сессия сохранена на диске). needs_2fa/failed сюда не
    входят намеренно — это состояния, где нужно действие человека, и
    автоматический повтор на каждый рестарт бота только бы спамил тем же
    сообщением."""
    async with aiosqlite.connect(state.DB_PATH) as db:
        async with db.execute(
            """
            SELECT owner_id, max_phone FROM tenant_accounts
            WHERE status IN ('awaiting_login', 'awaiting_group', 'active')
              AND max_phone IS NOT NULL
            """
        ) as cur:
            rows = await cur.fetchall()

    for owner_id, phone in rows:
        state.log.info(f"Переподключаю tenant'а {owner_id} после рестарта бота")
        asyncio.create_task(_run_login(owner_id, phone))


async def init_tenants_table():
    """Таблица-реестр живёт в основной bridge.db — это только метаданные
    (кто есть, какой телефон/статус/группа), не переписка."""
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tenant_accounts (
                owner_id INTEGER PRIMARY KEY,
                max_phone TEXT,
                status TEXT NOT NULL DEFAULT 'awaiting_phone',
                tg_group_id INTEGER,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        try:
            await db.execute("ALTER TABLE tenant_accounts ADD COLUMN max_2fa_password TEXT")
        except Exception:
            pass  # колонка уже есть
        await db.commit()


async def _get_tenant(owner_id: int) -> dict | None:
    async with aiosqlite.connect(state.DB_PATH) as db:
        async with db.execute(
            "SELECT owner_id, max_phone, status, tg_group_id, max_2fa_password FROM tenant_accounts WHERE owner_id = ?",
            (owner_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "owner_id": row[0],
                "max_phone": row[1],
                "status": row[2],
                "tg_group_id": row[3],
                "max_2fa_password": row[4],
            }


async def _set_status(owner_id: int, status: str, **fields):
    cols = ["status = ?", "updated_at = strftime('%s', 'now')"]
    values = [status]
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        values.append(v)
    values.append(owner_id)
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            f"UPDATE tenant_accounts SET {', '.join(cols)} WHERE owner_id = ?",
            values,
        )
        await db.commit()


async def send_group_instructions(tg_group_id: int):
    """Инструкция по командам — отдельным сообщением прямо в #General (без
    message_thread_id), чтобы была видна сразу при входе в группу, а не
    терялась в теме, где написали команду. Используется и при первом
    /setupgroup, и по явному /info (см. commands.py) — например, когда в
    группу зашёл новый человек и не видел исходное сообщение."""
    try:
        await state.tg_bot.send_message(
            tg_group_id,
            "📖 <b>Как пользоваться мостом</b>\n\n"
            "Каждый диалог и группа из MAX получают свою тему здесь — сообщения идут в обе стороны, "
            "прямо в этой теме.\n\n"
            "<code>/alias id слово</code> — задать псевдоним вместо id (если у контакта в MAX не "
            "заполнена фамилия, или чтобы не путать одноимённых). Пиши прямо в теме контакта, без "
            "аргументов покажет формат и уже заданные.\n\n"
            "<code>/ping слово</code> — задать слово, которое при появлении в MAX-сообщении "
            "будет превращаться в настоящее синее упоминание тебя здесь.\n\n"
            "<code>/retry</code> — переподключиться к MAX, если связь оборвалась.\n\n"
            "<code>/support текст</code> — написать мне, если что-то не так.\n\n"
            "<code>/info</code> — показать эту инструкцию ещё раз.",
            parse_mode="HTML",
        )
    except Exception as e:
        state.log.warning(f"Не удалось отправить инструкцию в General группы {tg_group_id}: {e}")


async def start_onboarding(owner_id: int):
    """Вызывается из access.py сразу после нажатия «Разрешить»."""
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO tenant_accounts (owner_id, status, updated_at)
            VALUES (?, 'awaiting_phone', strftime('%s', 'now'))
            ON CONFLICT(owner_id) DO UPDATE SET
                status = 'awaiting_phone',
                updated_at = strftime('%s', 'now')
            """,
            (owner_id,),
        )
        await db.commit()

    try:
        await state.tg_bot.send_message(
            owner_id,
            "Чтобы подключить свой MAX-аккаунт к этому боту, пришли номер телефона MAX "
            "(в международном формате, без плюса, например 79991234567).\n\n"
            "Если что-то не получается — напиши /support и своё сообщение, например:\n"
            "/support не приходит код",
        )
    except Exception as e:
        state.log.warning(f"start_onboarding: не удалось написать {owner_id}: {e}")


async def _handle_login_outcome_failure(owner_id: int, exc: Exception | None):
    if auth_flow.stored_password_rejected:
        await _forget_stored_password(owner_id)
    msg = str(exc) if exc else "неизвестная ошибка"
    error_code = (getattr(exc, "error", "") or "").lower()
    is_no2fa = "no2fa" in error_code or "2fa" in msg.lower()
    if is_no2fa:
        await _set_status(owner_id, "needs_2fa")
        await state.tg_bot.send_message(
            owner_id,
            "⚠️ Твой MAX-аккаунт требует включить облачный пароль (2FA) перед входом через бота.\n\n"
            "1) Открой приложение MAX\n"
            "2) Настройки → Безопасность → Облачный пароль → включи его\n"
            "3) Вернись сюда и напиши /retry",
        )
    else:
        await _set_status(owner_id, "failed")
        await state.tg_bot.send_message(
            owner_id,
            f"❌ Не получилось войти в MAX: {safe_error(msg)}\n"
            "Напиши /retry, чтобы попробовать ещё раз.",
        )
    state.tenant_clients.pop(owner_id, None)
    _run_tasks.pop(owner_id, None)


def safe_error(msg: str) -> str:
    return msg[:300]


async def _forget_stored_password(owner_id: int):
    state.tenant_2fa_passwords.pop(owner_id, None)
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            "UPDATE tenant_accounts SET max_2fa_password = NULL WHERE owner_id = ?",
            (owner_id,),
        )
        await db.commit()


async def _remember_password(owner_id: int, password: str):
    state.tenant_2fa_passwords[owner_id] = password
    async with aiosqlite.connect(state.DB_PATH) as db:
        await db.execute(
            "UPDATE tenant_accounts SET max_2fa_password = ? WHERE owner_id = ?",
            (password, owner_id),
        )
        await db.commit()


async def _send_group_setup_instructions(owner_id: int):
    await state.tg_bot.send_message(
        owner_id,
        "Теперь создай отдельную группу в Telegram:\n"
        "1) Создай новую группу\n"
        "2) Включи в её настройках «Темы» (Topics)\n"
        "3) Добавь туда этого бота и выдай ему права администратора\n"
        "4) Напиши в этой группе команду /setupgroup\n\n"
        "После этого каждый новый диалог из MAX будет приходить туда отдельной темой.",
    )


def _remember_password_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да", callback_data=f"remember2fa:yes:{owner_id}"),
        InlineKeyboardButton(text="Нет", callback_data=f"remember2fa:no:{owner_id}"),
    ]])


async def _run_login(owner_id: int, phone: str):
    """Оркестрация входа: держит login_lock только пока идёт сам обмен
    кодом/паролем (до срабатывания on_start либо до ошибки), дальше
    отпускает лок и оставляет client.start() жить своей жизнью в фоне."""
    os.makedirs(state.tenant_cache_dir(owner_id), exist_ok=True)
    # Схема (dialogs/messages/...) в собственной БД tenant'а — без этого
    # первое же входящее сообщение из MAX падает с "no such table: dialogs"
    # (db_path параметризован по каждому tenant'у, но схему в его файле
    # ещё нужно реально создать). Идемпотентно, безопасно звать на каждой
    # попытке логина/реконнекта.
    import db_messages
    await db_messages.init_db(db_path=state.tenant_db_path(owner_id))

    client = Client(phone=phone, work_dir=state.tenant_cache_dir(owner_id), session_name="main.db")
    state.tenant_clients[owner_id] = client

    login_done = asyncio.Event()

    @client.on_start()
    async def _on_tenant_start(c):
        login_done.set()

    # Сброс состояния предыдущей попытки + подгрузка сохранённого пароля
    # (если tenant раньше согласился его запомнить) в тёплый кэш, откуда
    # его синхронно читает auth_flow._tg_input().
    auth_flow.last_entered_password = None
    auth_flow._auto_filled_password_this_attempt = False
    auth_flow.stored_password_rejected = False
    tenant = await _get_tenant(owner_id)
    if tenant and tenant.get("max_2fa_password"):
        state.tenant_2fa_passwords[owner_id] = tenant["max_2fa_password"]
    else:
        state.tenant_2fa_passwords.pop(owner_id, None)

    # Таймаут чуть больше внутреннего окна ожидания кода в auth_flow (600с),
    # чтобы не отобрать лок раньше времени, пока человек ещё вводит код,
    # но и не держать лок вечно, если pymax вдруг зависнет без исключения.
    timed_out = False
    async with auth_flow.login_lock:
        auth_flow._active_login_target_id = owner_id
        start_task = asyncio.create_task(client.start())
        wait_done = asyncio.create_task(login_done.wait())
        try:
            await asyncio.wait_for(
                asyncio.wait({start_task, wait_done}, return_when=asyncio.FIRST_COMPLETED),
                timeout=650,
            )
        except asyncio.TimeoutError:
            timed_out = True
            start_task.cancel()
        finally:
            auth_flow._active_login_target_id = ADMIN_ID
            # Если код/пароль так и не пришёл (забросили попытку) — _tg_input
            # мог всё ещё сидеть в _auth_waiting=True на отдельном потоке.
            # Не сбросив это здесь, следующее ЛЮБОЕ личное сообщение админу
            # (даже команда) будет молча съедено как "код входа" — например
            # заброшенная регистрация оставляла этот флаг висеть и съедала
            # следующую команду админа.
            auth_flow._auth_waiting = False

    if login_done.is_set():
        wait_done.cancel()
        _run_tasks[owner_id] = start_task
        asyncio.create_task(_watch_tenant_task(owner_id, start_task))

        # Автоматический self-link: бот и так знает оба id (свой MAX id
        # клиента и свой же tg id владельца), так что упоминания тенанта
        # самого себя в MAX сразу приходят синим упоминанием в TG — без
        # ручного /link. Идемпотентно и безопасно звать на каждом входе,
        # включая тихие реконнекты после рестарта — так это применится и
        # для тенантов, зарегистрировавшихся до этой фичи.
        try:
            own_max_id = client.me.contact.id
            await db_meta.set_mention_link(own_max_id, "", owner_id, db_path=state.tenant_db_path(owner_id))
        except Exception as e:
            state.log.warning(f"Не удалось сделать self-link для tenant'а {owner_id}: {e}")

        # Уже была полностью настроена (группа привязана) — это не новый
        # вход, а тихое переподключение после рестарта бота: просто заново
        # навешиваем обработчики на новый Client и восстанавливаем маппинг
        # группы, без повторной инструкции "создай группу".
        prior_tg_group_id = tenant.get("tg_group_id") if tenant else None
        if tenant and tenant.get("status") == "active" and prior_tg_group_id:
            import relay_max_to_tg  # отложенный импорт, см. докстринг модуля
            relay_max_to_tg.register_tenant_handlers(
                client, tg_group_id=prior_tg_group_id, db_path=state.tenant_db_path(owner_id)
            )
            state.tenant_group_map[prior_tg_group_id] = owner_id
            await _set_status(owner_id, "active")
            # Добор пропущенного — теперь с границей по времени (last_seen_at,
            # см. relay_max_to_tg._backfill_missed_messages): без неё
            # добор пропущенного рассылал бы дубликатами недели старой
            # переписки.
            await relay_max_to_tg._backfill_missed_messages(
                client, tg_group_id=prior_tg_group_id, db_path=state.tenant_db_path(owner_id)
            )
            # Тихо — если реконнект прошёл успешно, тенанту не нужно об этом
            # знать, он ничего не заметил. Уведомляем только когда что-то
            # реально требует его действия (2FA, обрыв связи, сломанная группа).
            return

        await _set_status(owner_id, "awaiting_group")

        if auth_flow.stored_password_rejected:
            await _forget_stored_password(owner_id)

        if auth_flow.last_entered_password:
            _pending_remember_password[owner_id] = auth_flow.last_entered_password
            await state.tg_bot.send_message(
                owner_id,
                "✅ Вход в MAX выполнен!\n\n"
                "Запомнить облачный пароль, чтобы не спрашивать его при следующих входах?",
                reply_markup=_remember_password_keyboard(owner_id),
            )
        else:
            await state.tg_bot.send_message(owner_id, "✅ Вход в MAX выполнен!")
            await _send_group_setup_instructions(owner_id)
        return

    wait_done.cancel()
    if timed_out:
        await _handle_login_outcome_failure(owner_id, TimeoutError("не дождались кода/входа за 10 минут"))
        return

    # login_done не выставился -> start_task завершился ошибкой (или его отменили)
    exc = start_task.exception() if start_task.done() and not start_task.cancelled() else None
    await _handle_login_outcome_failure(owner_id, exc)


async def _watch_tenant_task(owner_id: int, task: asyncio.Task):
    """Если уже подключённый клиент tenant'а всё же упадёт позже (не во
    время логина, а в процессе работы) — не молчим, сообщаем владельцу."""
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception as e:
        state.log.warning(f"MAX-клиент tenant'а {owner_id} завершился с ошибкой: {e}")
        state.tenant_clients.pop(owner_id, None)
        _run_tasks.pop(owner_id, None)
        try:
            await state.tg_bot.send_message(
                owner_id,
                f"⚠️ Соединение с MAX прервалось: {safe_error(str(e))}\nНапиши /retry, чтобы переподключиться.",
            )
        except Exception:
            pass


def _is_valid_phone(text: str) -> bool:
    t = text.strip().lstrip("+")
    return t.isdigit() and 10 <= len(t) <= 15


@state.dp.message(lambda m: m.chat.type == "private" and m.from_user is not None)
async def tenant_onboarding_input(message: TgMessage):
    """Ловит номер телефона (статус awaiting_phone) и /retry — остальное
    пропускает дальше по цепочке обработчиков (SkipHandler)."""
    owner_id = message.from_user.id
    if owner_id == ADMIN_ID:
        raise SkipHandler()

    tenant = await _get_tenant(owner_id)
    if not tenant:
        raise SkipHandler()

    text = (message.text or "").strip()

    if text == "/retry" and tenant["status"] in ("needs_2fa", "failed", "awaiting_group", "active"):
        # needs_2fa/failed — вход не завершился, нужна новая попытка логина
        # с нуля. А вот active/awaiting_group — вход уже был успешен и
        # (для active) группа уже привязана, нужен просто тихий реконнект
        # по сохранённой сессии. Сбрасывать статус тут в awaiting_login
        # было ошибкой: _run_login ниже читает текущий статус именно чтобы
        # отличить тихий реконнект от нового онбординга (ветка "Уже была
        # полностью настроена") — искусственный сброс заставлял её всегда
        # идти по пути "заново привяжи группу" и пропускать добор
        # пропущенных сообщений, даже когда группа давно привязана (живой
        # случай: /retry после обрыва связи откатывал active → awaiting_group).
        if tenant["status"] in ("needs_2fa", "failed"):
            await _set_status(owner_id, "awaiting_login")
        await message.reply("⏳ Пробую войти снова…")
        asyncio.create_task(_run_login(owner_id, tenant["max_phone"]))
        return

    if tenant["status"] == "awaiting_phone":
        if not text or not _is_valid_phone(text):
            await message.reply(
                "Похоже на неверный формат.\n"
                "Пришли номер телефона MAX цифрами, без плюса (например 79991234567)."
            )
            return
        phone = text.lstrip("+")
        await _set_status(owner_id, "awaiting_login", max_phone=phone)
        await message.reply("⏳ Пробую войти, жди код или пароль в этом чате…")
        asyncio.create_task(_run_login(owner_id, phone))
        return

    # Во всех остальных статусах (awaiting_login/needs_2fa/awaiting_group/active/failed)
    # это сообщение — не про онбординг (может быть код для auth_flow, или просто
    # переписка), передаём дальше.
    raise SkipHandler()


@state.dp.message(lambda m: m.text and m.text.startswith("/setupgroup"))
async def cmd_linkgroup(message: TgMessage):
    if not message.from_user:
        return
    owner_id = message.from_user.id
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Команду нужно писать в самой группе, которую привязываешь.")
        return

    tenant = await _get_tenant(owner_id)
    if not tenant or tenant["status"] != "awaiting_group":
        await message.reply(
            "Нечего привязывать: либо ты ещё не входил в MAX через этого бота, "
            "либо группа уже привязана."
        )
        return

    if not getattr(message.chat, "is_forum", False):
        await message.reply(
            "В этой группе не включены «Темы» (Topics).\n"
            "Настройки группы → Темы → включить, потом снова напиши /setupgroup."
        )
        return

    try:
        me = await state.tg_bot.get_me()
        member = await state.tg_bot.get_chat_member(message.chat.id, me.id)
    except Exception as e:
        await message.reply(f"Не удалось проверить права бота в группе: {e}")
        return

    if member.status not in ("administrator", "creator"):
        await message.reply(
            "Бот не администратор в этой группе.\n"
            "Выдай ему права администратора и снова напиши /setupgroup."
        )
        return

    if getattr(member, "can_manage_topics", None) is False:
        await message.reply(
            "У бота есть админка, но не включено право «Управление темами» (Manage Topics).\n"
            "Включи это право и снова напиши /setupgroup."
        )
        return

    client = state.tenant_clients.get(owner_id)
    if client is None:
        await message.reply(
            "Похоже, бот перезапускался после твоего входа в MAX, и соединение потерялось.\n"
            "Напиши мне в личку /retry — переподключусь, используя тот же вход без нового кода."
        )
        return

    tg_group_id = message.chat.id
    db_path = state.tenant_db_path(owner_id)

    import relay_max_to_tg  # отложенный импорт — см. докстринг модуля
    relay_max_to_tg.register_tenant_handlers(client, tg_group_id=tg_group_id, db_path=db_path)

    state.tenant_group_map[tg_group_id] = owner_id
    await _set_status(owner_id, "active", tg_group_id=tg_group_id)

    # Здесь диалогов ещё нет (свежий тенант) — вызов безвредный no-op, но
    # заодно и проставит первую метку last_seen_at.
    await relay_max_to_tg._backfill_missed_messages(client, tg_group_id=tg_group_id, db_path=db_path)

    await message.reply(
        "✅ Готово! Эта группа привязана к твоему MAX-аккаунту.\n"
        "Новые диалоги из MAX будут приходить сюда отдельными темами, "
        "а ответы в темах — уходить обратно в MAX."
    )

    # Инструкция — отдельным сообщением прямо в #General (без message_thread_id),
    # чтобы была видна сразу при входе в группу, а не терялась в теме, где
    # случайно написали /setupgroup.
    await send_group_instructions(tg_group_id)

    try:
        uname = f"@{message.from_user.username}" if message.from_user.username else "—"
        await state.tg_bot.send_message(
            ADMIN_ID,
            f"✅ <b>Регистрация завершена</b>\n\n"
            f"{uname} <code>{owner_id}</code> привязал группу и теперь пользуется мостом.",
            parse_mode="HTML",
        )
    except Exception:
        pass


@state.dp.callback_query(lambda c: c.data and c.data.startswith("remember2fa:"))
async def cb_remember_2fa(query: CallbackQuery):
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Ошибка данных")
        return
    _, answer, owner_id_s = parts
    try:
        owner_id = int(owner_id_s)
    except ValueError:
        await query.answer("Плохой id")
        return

    if not query.from_user or query.from_user.id != owner_id:
        await query.answer("Это не твоя кнопка", show_alert=True)
        return

    password = _pending_remember_password.pop(owner_id, None)

    if answer == "yes" and password:
        await _remember_password(owner_id, password)
        await query.answer("Запомнил")
        if query.message:
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
    else:
        await query.answer("Не буду запоминать")
        if query.message:
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    await _send_group_setup_instructions(owner_id)


@state.dp.message(lambda m: m.text and m.text.startswith("/alias"))
async def cmd_tenant_alias(message: TgMessage):
    """/alias для tenant'ов — работает только внутри их же привязанной
    группы, пишет в их же изолированную БД (commands.py's cmd_alias
    пропускает сюда через SkipHandler для не-админов)."""
    if not message.from_user:
        return
    owner_id = state.tenant_group_map.get(message.chat.id)
    if owner_id is None or message.from_user.id != owner_id:
        return

    db_path = state.tenant_db_path(owner_id)

    parts = (message.text or "").strip().split()
    if len(parts) == 3:
        try:
            max_user_id = int(parts[1])
            last = parts[2]
        except ValueError:
            await message.reply(
                "Формат:\n"
                "/alias <id> <фамилия>\n"
                "/alias <id> 0\n"
                "/alias <id> -\n"
                "Пример:\n"
                "/alias 987654321 Красавчик"
            )
            return
    elif len(parts) == 4:
        try:
            max_user_id = int(parts[2])
            last = parts[3]
        except ValueError:
            await message.reply("id должен быть числом")
            return
    else:
        # Если команду написали прямо в теме конкретного личного диалога —
        # в примере подставляем реальный MAX user id собеседника (не
        # max_chat_id — это разные числа, см. докстринг колонки
        # max_sender_id в db_messages.init_db).
        import db_messages  # отложенный импорт, см. докстринг _run_login выше
        example_id = 987654321
        try:
            dialog = await db_messages.get_dialog_by_thread(
                message.chat.id, message.message_thread_id, db_path=db_path
            )
            if dialog and dialog["max_sender_id"]:
                example_id = dialog["max_sender_id"]
        except Exception:
            pass

        lines = [
            "Формат:",
            f"<code>/alias {example_id} Красавчик</code> — фамилия",
            f"<code>/alias {example_id} 0</code> — только имя, без id",
            f"<code>/alias {example_id} -</code> — сбросить (снова будет id)",
            "или:",
            f"<code>/alias Александр {example_id} Красавчик</code>",
        ]

        existing = await db_meta.list_aliases(db_path=db_path)
        if existing:
            lines.append("\n<b>Уже заданы:</b>")
            for mid, last in existing:
                lines.append(f"<code>{mid}</code> → {safe_html(last)}")

        await message.reply("\n".join(lines), parse_mode="HTML")
        return

    if last == "-":
        await db_meta.delete_alias(max_user_id, db_path=db_path)
        import relay_tg_to_max  # отложенный импорт, см. докстринг relay_tg_to_max.py
        await relay_tg_to_max.rename_topic_for_alias(max_user_id, None, db_path=db_path)
        await message.reply(f"🗑 Алиас для <code>{max_user_id}</code> сброшен", parse_mode="HTML")
        return

    if last != "0" and not last.strip():
        await message.reply("Укажи фамилию или 0")
        return

    await db_meta.set_alias(max_user_id, last.strip(), db_path=db_path)
    import relay_tg_to_max  # отложенный импорт, см. докстринг relay_tg_to_max.py
    await relay_tg_to_max.rename_topic_for_alias(max_user_id, last.strip(), db_path=db_path)
    if last == "0":
        await message.reply(f"✅ <code>{max_user_id}</code> → только имя (без id)", parse_mode="HTML")
    else:
        await message.reply(
            f"✅ <code>{max_user_id}</code> → фамилия <b>{safe_html(last)}</b>",
            parse_mode="HTML",
        )


@state.dp.message(lambda m: m.text and m.text.startswith("/ping"))
async def cmd_tenant_ping(message: TgMessage):
    """/ping для tenant'ов — слово/фраза → TG-упоминание в ИХ собственных
    сообщениях (своя изолированная ping_map, не общая с тобой). Сделан
    общей командой вместо /link — /link остался только твоим, а этот
    более простой и понятный тенантам ("любое слово →
    упоминание"). Не привязан к конкретной группе/теме (commands.py's
    cmd_ping пропускает сюда через SkipHandler для не-админов)."""
    if not message.from_user:
        return
    owner_id = message.from_user.id
    if owner_id not in state.tenant_group_map.values():
        return

    db_path = state.tenant_db_path(owner_id)
    parts = (message.text or "").strip().split()

    if len(parts) == 1:
        rows = await db_meta.list_pings(db_path=db_path)
        if not rows:
            await message.reply("Словарь пуст.\nФормат: /ping слово — запомнить слово для себя")
            return
        lines = []
        for row in rows:
            k, u = row[0], row[1]
            tid = row[2] if len(row) > 2 else None
            mark = f"id={tid}" if tid else "нет id"
            lines.append(f"<code>{safe_html(k)}</code> → @{safe_html(u)} ({mark})")
        await message.reply("<b>Ping-словарь:</b>\n" + "\n".join(lines), parse_mode="HTML")
        return

    # /ping слово — без второго аргумента: бот и так знает твой tg id,
    # незачем заставлять искать/угадывать чужой username — он часто вообще
    # не резолвится через Bot API.
    if len(parts) == 2:
        keyword = parts[1]
        if keyword == "-":
            await message.reply("Укажи слово, которое хочешь удалить: /ping слово -")
            return
        await db_meta.set_ping(keyword, "", owner_id, db_path=db_path)
        await message.reply(
            f"✅ <code>{safe_html(keyword.lower())}</code> → теперь упоминает тебя",
            parse_mode="HTML",
        )
        return

    if len(parts) != 3:
        await message.reply(
            "Формат:\n"
            "<code>/ping слово</code> — упоминать тебя по этому слову\n"
            "<code>/ping слово -</code> — удалить\n"
            "<code>/ping слово @username</code> — упоминать кого-то другого (только если он уже писал боту)\n"
            "/ping — список",
            parse_mode="HTML",
        )
        return

    keyword = parts[1]
    value = parts[2]

    if value == "-":
        await db_meta.delete_ping(keyword, db_path=db_path)
        await message.reply(f"🗑 Удалено: <code>{safe_html(keyword)}</code>", parse_mode="HTML")
        return

    uname = value.strip().lstrip("@")
    tg_id = None

    if uname.isdigit():
        tg_id = int(uname)
        uname = ""
    else:
        try:
            chat = await state.tg_bot.get_chat(f"@{uname}")
            tg_id = int(chat.id)
        except Exception as e:
            state.log.warning(f"get_chat @{uname}: {e}")
            try:
                await state.tg_bot.send_message(
                    owner_id,
                    (
                        "⚠️ Не удалось узнать TG id\n"
                        f"слово: <code>{safe_html(keyword)}</code>\n"
                        f"username: @{safe_html(uname)}\n"
                        f"ошибка: <code>{safe_html(str(e))}</code>\n\n"
                        f"Можно так:\n<code>/ping {safe_html(keyword)} 123456789</code>"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    await db_meta.set_ping(keyword, uname, tg_id, db_path=db_path)
    if tg_id:
        await message.reply(
            f"✅ <code>{safe_html(keyword.lower())}</code> → "
            f"@{safe_html(uname)} / <code>{tg_id}</code>\n"
            "В чате будет синее исходное слово + уведомление",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"⚠️ <code>{safe_html(keyword.lower())}</code> → @{safe_html(uname)}\n"
            "id не найден, пока будет обычный @username",
            parse_mode="HTML",
        )
