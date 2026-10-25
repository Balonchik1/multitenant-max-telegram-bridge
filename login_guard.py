"""
Предохранитель от бесконечного заказа SMS-кодов (основной аккаунт и подключённые пользователи).

Когда MAX отзывает вход (завершает сессии аккаунта), pymax начинает вход заново:
заказывает SMS, ждёт код 10 минут, по таймауту повторяет, и так по кругу. На телефон
владельца аккаунта сыплются SMS, а MAX может временно заблокировать вход по номеру.

Здесь вход по SMS разрешён, только если:
  - это первый вход в этот аккаунт на этой установке (раньше он ни разу не удавался), или
  - владелец вручную разрешил один заказ кода: основной аккаунт командой /relogin,
    подключённый пользователь командой /retry (разрешение разовое).
Иначе вход не запускается: бот сообщает владельцу, что вход слетел, а остальной мост
продолжает работать.

Состояние хранится в двух пустых файлах в папке сессии (у каждого аккаунта своя):
  login_ok.marker  вход хотя бы раз удавался
  allow_sms.flag   разовое разрешение на один заказ SMS
"""
import os

from pymax import ConsoleSmsCodeProvider, SmsAuthFlow

CACHE_DIR = "cache"  # папка сессии основного аккаунта
_MARKER_NAME = "login_ok.marker"
_PERMIT_NAME = "allow_sms.flag"


class LoginNotAllowed(RuntimeError):
    """Вход по SMS сейчас не разрешён: нужна команда владельца (/relogin или /retry)."""


def _marker_path(cache_dir: str | None) -> str:
    return os.path.join(cache_dir or CACHE_DIR, _MARKER_NAME)


def _permit_path(cache_dir: str | None) -> str:
    return os.path.join(cache_dir or CACHE_DIR, _PERMIT_NAME)


def mark_login_ok(cache_dir: str | None = None) -> None:
    """Вход удался: дальше автоматический заказ SMS запрещён. Лишнее
    разрешение на всякий случай убираем, чтобы оно не сработало потом само."""
    os.makedirs(cache_dir or CACHE_DIR, exist_ok=True)
    with open(_marker_path(cache_dir), "a"):
        pass
    try:
        os.remove(_permit_path(cache_dir))
    except FileNotFoundError:
        pass


def grant_permission(cache_dir: str | None = None) -> None:
    """Разрешает ОДИН заказ SMS."""
    os.makedirs(cache_dir or CACHE_DIR, exist_ok=True)
    with open(_permit_path(cache_dir), "a"):
        pass


def sms_allowed(cache_dir: str | None = None) -> bool:
    if not os.path.exists(_marker_path(cache_dir)):
        return True  # первый вход: войти по SMS нужно
    try:
        os.remove(_permit_path(cache_dir))
    except FileNotFoundError:
        return False
    return True


class GuardedSmsAuthFlow(SmsAuthFlow):
    """Обычный SMS-вход pymax, но без самовольного заказа кода."""

    def __init__(self, code_provider, password_provider=None, cache_dir: str | None = None):
        super().__init__(code_provider, password_provider)
        self.cache_dir = cache_dir

    async def authenticate(self, app):
        if not sms_allowed(self.cache_dir):
            raise LoginNotAllowed("вход слетел, заказ SMS-кода нужно разрешить командой владельца")
        return await super().authenticate(app)


def build_auth_flow(cache_dir: str | None = None) -> GuardedSmsAuthFlow:
    # Как стандартный вход Client: код читается через input(), его подменяет auth_flow.py
    return GuardedSmsAuthFlow(ConsoleSmsCodeProvider(), cache_dir=cache_dir)
