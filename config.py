"""Конфигурация бота — все значения берутся из переменных окружения
(.env в корне проекта, туда же откуда venv/cache — см. .env.example
для списка нужных переменных и их описания). Секреты никогда не лежат
в этом файле и не попадают в git (.env в .gitignore)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Не задана переменная окружения {name} — проверь .env (см. .env.example)")
    return value


TG_TOKEN = _env("TG_TOKEN")
TG_GROUP_ID = int(_env("TG_GROUP_ID"))
TG_GROUP_FLAT = int(_env("TG_GROUP_FLAT"))
FLAT_MAX_CHAT_ID = int(_env("FLAT_MAX_CHAT_ID"))

ALLOWED_GROUPS = {
    TG_GROUP_ID,
    TG_GROUP_FLAT,
}

ADMIN_ID = int(_env("ADMIN_ID"))
MAX_PHONE = _env("MAX_PHONE")
# Необязательно: короткий путь для ТВОЕГО собственного входа (см. auth_flow.py) —
# без него просто каждый раз спросит пароль в личке, как и для любого tenant'а.
MAX_2FA_PASSWORD = _env("MAX_2FA_PASSWORD", required=False)

# Необязательно: ключ для шифрования периодических бэкапов файлов сессии
# (см. session_backup.py) — если не задан, автобэкапы просто не делаются.
# Защищает СКОПИРОВАННЫЕ данные (например если архив бэкапа утечёт), не
# живую рабочую сессию — см. докстринг session_backup.py.
SESSION_BACKUP_KEY = _env("SESSION_BACKUP_KEY", required=False)
