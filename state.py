"""
Общее состояние процесса: клиенты, конфиг рантайма, буферы.

Все переменные из этого модуля, которые где-либо переприсваиваются
(allowed_groups_runtime, _main_loop), нужно читать и писать только через
`import state; state.xxx` — НЕ через `from state import xxx`. Если сделать
`from state import allowed_groups_runtime`, то после того как main()
переприсвоит `state.allowed_groups_runtime = ...`, модуль, сделавший такой
импорт, продолжит смотреть на старый (пустой) набор.
"""
import asyncio
import logging
import os
from collections import defaultdict

from aiogram import Dispatcher, Bot
from pymax import Client

from config import TG_TOKEN, MAX_PHONE, ADMIN_ID
from access import setup_access
from db_meta import set_db_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("bridge")

DB_PATH = "bridge.db"

# MAX клиент
max_client = Client(
    phone=MAX_PHONE,
    work_dir="cache",
    session_name="main.db",
)

# Telegram бот
tg_bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

set_db_path(DB_PATH)
setup_access(dp, tg_bot, DB_PATH, ADMIN_ID)

# --- Рантайм-состояние (переприсваивается в bridge.main()) ---
allowed_groups_runtime: set[int] = set()
_main_loop: asyncio.AbstractEventLoop | None = None

# --- Буферы/очереди (создаются один раз, дальше только мутируются) ---
topic_locks = defaultdict(asyncio.Lock)
tg_queue: asyncio.Queue = asyncio.Queue()
_recent_max_msgs: dict[tuple, float] = {}
media_group_buffer: dict[str, list] = {}
media_group_timers: dict[str, asyncio.Task] = {}

MAX_FILE_SIZE = 50 * 1024 * 1024  # лимит Telegram Bot API на ОТПРАВКУ файла ботом
# Отдельный, более строгий лимит на СКАЧИВАНИЕ файла ботом через Bot API —
# 20 МБ, а не 50 (два разных официальных лимита Telegram). Раньше TG→MAX
# сверялась с MAX_FILE_SIZE и пропускала файлы 20-50 МБ, которые Telegram
# потом отказывался отдавать при скачивании ("file is too big") — ошибка
# нигде не показывалась.
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
MEDIA_GROUP_DELAY = 1.1  # секунды ожидания остальных частей альбома

# --- Мультитенантность (tenants.py) ---
# Живёт здесь, а не в tenants.py, чтобы relay_tg_to_max.py могло читать это
# без циклического импорта (tenants.py -> relay_max_to_tg.py -> relay_tg_to_max.py
# -> (было бы) tenants.py).
TENANTS_ROOT = "tenants"
tenant_clients: dict[int, Client] = {}       # owner_id -> pymax Client
tenant_group_map: dict[int, int] = {}        # tg_group_id -> owner_id
tenant_2fa_passwords: dict[int, str] = {}    # owner_id -> сохранённый облачный пароль (тёплый кэш из БД)


def tenant_db_path(owner_id: int) -> str:
    return os.path.join(TENANTS_ROOT, str(owner_id), "bridge.db")


def tenant_cache_dir(owner_id: int) -> str:
    return os.path.join(TENANTS_ROOT, str(owner_id), "cache")
