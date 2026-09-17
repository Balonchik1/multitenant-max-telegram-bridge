"""
Шифрованные бэкапы файлов сессии MAX (cache/ у админа, tenants/<id>/cache/
у каждого тенанта) — защита СКОПИРОВАННЫХ данных (например если архив
бэкапа случайно утечёт или попадёт в публичное хранилище), а не живого
рабочего файла: пока бот работает, pymax должен читать/писать сессию в
открытом виде — это ограничение самого pymax, шифровать активно
используемый файл бессмысленно (ключ всё равно будет в памяти того же
процесса). Подробное обсуждение см. в истории — раздел про монетизацию/
кастодиальный риск.

Формат зашифрованного файла: 16 байт соли (PBKDF2) + 12 байт nonce (AES-GCM)
+ ciphertext. Ключ шифрования выводится из SESSION_BACKUP_KEY (config.py,
необязательная переменная — если не задана, бэкапы просто не делаются).
"""
import io
import os
import tarfile
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_PBKDF2_ITERATIONS = 600_000
_SALT_LEN = 16
_NONCE_LEN = 12


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_PBKDF2_ITERATIONS)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, data, None)
    return salt + nonce + ciphertext


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    salt = blob[:_SALT_LEN]
    nonce = blob[_SALT_LEN:_SALT_LEN + _NONCE_LEN]
    ciphertext = blob[_SALT_LEN + _NONCE_LEN:]
    key = _derive_key(passphrase, salt)
    return AESGCM(key).decrypt(nonce, ciphertext, None)


def _tar_directory(dir_path: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(dir_path, arcname=os.path.basename(os.path.normpath(dir_path)))
    return buf.getvalue()


def _untar_bytes(raw: bytes, extract_to: str):
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        # filter="data" — новый (с 3.12) безопасный режим распаковки, без
        # него Python 3.14 сам начнёт применять его с предупреждением.
        tar.extractall(path=extract_to, filter="data")


def backup_session_dir(dir_path: str, dest_path: str, passphrase: str) -> bool:
    """Архивирует директорию сессии (cache/) и шифрует результат в один
    файл. Возвращает False (без ошибки), если dir_path ещё не существует —
    нормальная ситуация для тенанта, который ещё не успел залогиниться."""
    if not os.path.isdir(dir_path):
        return False
    raw = _tar_directory(dir_path)
    encrypted = encrypt_bytes(raw, passphrase)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(encrypted)
    return True


def restore_session_dir(backup_path: str, extract_to: str, passphrase: str):
    """Расшифровывает и распаковывает бэкап обратно — извлекает в
    extract_to (родительскую директорию, архив сам содержит имя папки,
    например "cache/"). Ручная операция восстановления, не вызывается
    автоматически ботом."""
    with open(backup_path, "rb") as f:
        encrypted = f.read()
    raw = decrypt_bytes(encrypted, passphrase)
    _untar_bytes(raw, extract_to)


def _prune_old_backups(backups_root: str, name_prefix: str, keep: int):
    if keep <= 0:
        return
    candidates = sorted(
        f for f in os.listdir(backups_root)
        if f.startswith(f"{name_prefix}-") and f.endswith(".enc")
    )
    for old in candidates[:-keep]:
        try:
            os.remove(os.path.join(backups_root, old))
        except OSError:
            pass


def backup_all_sessions(
    backups_root: str,
    passphrase: str,
    tenants_root: str,
    admin_cache_dir: str = "cache",
    keep: int = 3,
    on_error=None,
) -> list[str]:
    """Бэкапит cache/ админа и cache/ каждого тенанта в tenants_root, храня
    только последние `keep` версий на каждого. Одна сломанная директория
    не должна останавливать бэкап остальных — ошибки идут через on_error
    (callback(name, exception)), а не поднимаются наружу."""
    created = []
    ts = time.strftime("%Y%m%d-%H%M%S")

    def _do_one(dir_path: str, name: str):
        dest = os.path.join(backups_root, f"{name}-{ts}.enc")
        try:
            if backup_session_dir(dir_path, dest, passphrase):
                created.append(dest)
                _prune_old_backups(backups_root, name, keep)
        except Exception as e:
            if on_error:
                on_error(name, e)

    _do_one(admin_cache_dir, "admin")
    if os.path.isdir(tenants_root):
        for owner_id in sorted(os.listdir(tenants_root)):
            _do_one(os.path.join(tenants_root, owner_id, "cache"), f"tenant-{owner_id}")

    return created
