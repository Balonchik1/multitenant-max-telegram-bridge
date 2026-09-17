"""
Вход в MAX без интерактивного терминала: библиотека pymax зовёт
builtins.input()/getpass.getpass() для кода/пароля — здесь это
подменяется на пересылку запроса в личку и ожидание ответа.

builtins.input()/getpass.getpass() — подмена ГЛОБАЛЬНАЯ на весь процесс,
поэтому одновременно может идти только один вход (админский или
чей-то из tenants.py) — это гарантирует _login_lock. `_active_login_target_id`
говорит, кому сейчас адресовать запрос кода/пароля: по умолчанию ADMIN_ID
(старое поведение не меняется), а на время логина конкретного tenant'а
tenants.py временно выставляет сюда его tg id.
"""
import asyncio
import builtins
import getpass
import html
import queue

from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message as TgMessage

from config import ADMIN_ID, MAX_2FA_PASSWORD
from formatters import safe_html
import state

_auth_input_queue: queue.Queue[str] = queue.Queue()
_auth_waiting = False
_auth_future: asyncio.Future | None = None

# Кому сейчас адресовать запрос кода/пароля (по умолчанию — админу, как раньше).
_active_login_target_id: int = ADMIN_ID
# Гарантирует, что одновременно логинится только один аккаунт (админский или
# чей-то из tenants) — иначе два параллельных input() запутались бы, кому
# какой код адресовать.
login_lock = asyncio.Lock()

# --- Состояние текущей попытки логина, читает/сбрасывает tenants.py вокруг
# каждого login_lock (безопасно, т.к. одновременно идёт только одна попытка) ---
# Пароль, который tenant реально ввёл сам (не из сохранённого/config) в этой
# попытке — если не None после успешного входа, tenants.py предложит "запомнить?".
last_entered_password: str | None = None
# Уже подставляли сохранённый пароль tenant'а в этой попытке?
_auto_filled_password_this_attempt: bool = False
# Сохранённый пароль подставили, но MAX спросил снова -> пароль сменили/неверен.
stored_password_rejected: bool = False


async def _notify_admin_for_input(prompt: str):
    global _auth_waiting
    _auth_waiting = True
    try:
        text = (
            "🔐 <b>Вход в MAX</b>\n\n"
            f"<code>{html.escape(prompt.strip() or 'Введите код / пароль')}</code>\n\n"
            "Пришли следующим сообщением <b>только код или пароль</b>."
        )
        await state.tg_bot.send_message(_active_login_target_id, text, parse_mode="HTML")
    except Exception as e:
        state.log.error(f"Не удалось написать {_active_login_target_id} для auth: {e}")


def _tg_input(prompt: str = "") -> str:
    global last_entered_password, _auto_filled_password_this_attempt, stored_password_rejected, _auth_waiting

    p = (prompt or "").lower()
    is_password_prompt = any(x in p for x in ("password", "2fa", "парол", "cloud"))

    # MAX_2FA_PASSWORD — ТВОЙ личный облачный пароль из config.py. Используем
    # его как short-cut ТОЛЬКО когда логинится сам админ — иначе для входа
    # tenant'а сюда подставлялся бы админский пароль на чужой аккаунт, что
    # привело бы к попытке входа с заведомо неверным паролем.
    if _active_login_target_id == ADMIN_ID and MAX_2FA_PASSWORD and is_password_prompt:
        state.log.info("2FA: пароль взят из config")
        return MAX_2FA_PASSWORD.strip()

    # Сохранённый (с согласия tenant'а) пароль — подставляем один раз за
    # попытку. Если MAX спросит пароль СНОВА в той же попытке — значит
    # сохранённый не подошёл (сменили), больше не подставляем, идём
    # спрашивать вживую и помечаем, что сохранённое значение нужно стереть.
    if is_password_prompt and _active_login_target_id != ADMIN_ID:
        stored = state.tenant_2fa_passwords.get(_active_login_target_id)
        if stored:
            if not _auto_filled_password_this_attempt:
                _auto_filled_password_this_attempt = True
                state.log.info("2FA: пароль взят из сохранённых для tenant'а")
                return stored
            stored_password_rejected = True

    try:
        if state._main_loop is not None and state._main_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                _notify_admin_for_input(prompt),
                state._main_loop,
            )
    except Exception as e:
        state.log.warning(f"tg input notify: {e}")

    try:
        value = _auth_input_queue.get(timeout=600)
    except queue.Empty:
        _auth_waiting = False
        raise TimeoutError("Нет кода от ADMIN за 10 минут")
    value = value.strip()
    if is_password_prompt:
        last_entered_password = value
    return value


builtins.input = _tg_input
getpass.getpass = _tg_input


async def ask_admin_tg(prompt: str) -> str:
    """Пишет ADMIN_ID в личку и ждёт один текстовый ответ."""
    global _auth_future
    await state.tg_bot.send_message(ADMIN_ID, prompt)
    loop = asyncio.get_running_loop()
    _auth_future = loop.create_future()
    try:
        return await asyncio.wait_for(_auth_future, timeout=600)  # 10 мин
    finally:
        _auth_future = None


@state.dp.message(lambda m: m.chat.type == "private" and m.from_user and m.from_user.id == _active_login_target_id)
async def admin_private_auth(message: TgMessage):
    global _auth_waiting
    if not _auth_waiting or not message.text or message.text.startswith("/"):
        raise SkipHandler()  # передать /ping, /groups и т.д. — код/пароль от MAX
        # командой никогда не бывает, так что даже если _auth_waiting завис
        # True по ошибке, команды это больше не затронет.
    _auth_input_queue.put(message.text.strip())
    _auth_waiting = False
    await message.reply("✅ Принято, продолжаю вход…")


@state.dp.message(lambda m: m.chat.type == "private" and m.from_user and m.from_user.id == ADMIN_ID and (m.forward_from or m.forward_origin))
async def admin_forward_id(message: TgMessage):
    src = message.forward_from
    if src is None and getattr(message, "forward_origin", None) is not None:
        src = getattr(message.forward_origin, "sender_user", None)

    if src is None:
        await message.reply(
            "Переслали, но профиль скрыт настройками конфиденциальности.\n"
            "Id из такого сообщения Telegram боту не отдаёт."
        )
        return

    uname = f"@{src.username}" if src.username else "без username"
    name = src.full_name or src.first_name or "—"
    await message.reply(
        f"👤 {safe_html(name)}\n"
        f"username: {safe_html(uname)}\n"
        f"id: <code>{src.id}</code>\n\n"
        f"Привязать:\n<code>/ping Слово {src.id}</code>",
        parse_mode="HTML",
    )


@state.dp.message(lambda m: m.chat.type == "private" and m.from_user and m.from_user.id == ADMIN_ID)
async def admin_auth_reply(message: TgMessage):
    global _auth_future
    if _auth_future is not None and not _auth_future.done() and message.text:
        _auth_future.set_result(message.text.strip())
        await message.reply("✅ Принято")
        return
    raise SkipHandler()
