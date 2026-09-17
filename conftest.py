"""
Общий conftest для всего репозитория.

ВАЖНО: переменные окружения выставляются здесь, ДО импорта любых модулей
проекта — config.py требует их наличия (RuntimeError, если переменная не
задана), а state.py на импорте создаёт реальные объекты Bot()/Client()
(без сетевых вызовов, но с валидацией формата токена). Тесты никогда не
должны использовать настоящие креды, даже если рядом лежит настоящий
.env — иначе можно случайно достучаться до реального бота/MAX-аккаунта.
setdefault() ничего не перезапишет, если переменная уже стоит в окружении
явно (например через CI secrets), но override=False в load_dotenv()
внутри config.py всё равно не даст .env перебить то, что мы зададим здесь.
"""
import os

os.environ.setdefault("TG_TOKEN", "123456789:AAEaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
os.environ.setdefault("TG_GROUP_ID", "-1000000000001")
os.environ.setdefault("TG_GROUP_FLAT", "-1000000000002")
os.environ.setdefault("FLAT_MAX_CHAT_ID", "1")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("MAX_PHONE", "+70000000000")
