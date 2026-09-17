import os

import pytest

import session_backup as sb


def test_encrypt_decrypt_roundtrip():
    data = b"secret session bytes \x00\x01\x02"
    blob = sb.encrypt_bytes(data, "correct passphrase")
    assert sb.decrypt_bytes(blob, "correct passphrase") == data


def test_decrypt_fails_with_wrong_passphrase():
    blob = sb.encrypt_bytes(b"secret", "right")
    with pytest.raises(Exception):
        sb.decrypt_bytes(blob, "wrong")


def test_backup_and_restore_session_dir_roundtrip(tmp_path):
    src = tmp_path / "cache"
    src.mkdir()
    (src / "main.db").write_bytes(b"fake pymax session bytes")
    (src / "nested").mkdir()
    (src / "nested" / "extra.txt").write_text("hello")

    backup_file = tmp_path / "backups" / "admin-20260101-000000.enc"
    ok = sb.backup_session_dir(str(src), str(backup_file), "pw")
    assert ok is True
    assert backup_file.exists()

    # содержимое на диске должно быть НЕ читаемо как обычный tar/текст
    raw_backup = backup_file.read_bytes()
    assert b"fake pymax session bytes" not in raw_backup

    restore_to = tmp_path / "restored"
    restore_to.mkdir()
    sb.restore_session_dir(str(backup_file), str(restore_to), "pw")

    restored_db = restore_to / "cache" / "main.db"
    assert restored_db.read_bytes() == b"fake pymax session bytes"
    assert (restore_to / "cache" / "nested" / "extra.txt").read_text() == "hello"


def test_backup_session_dir_returns_false_for_missing_dir(tmp_path):
    missing = tmp_path / "does_not_exist"
    dest = tmp_path / "backups" / "x.enc"
    assert sb.backup_session_dir(str(missing), str(dest), "pw") is False
    assert not dest.exists()


def test_backup_all_sessions_admin_and_tenants(tmp_path):
    admin_cache = tmp_path / "cache"
    admin_cache.mkdir()
    (admin_cache / "main.db").write_bytes(b"admin session")

    tenants_root = tmp_path / "tenants"
    (tenants_root / "111" / "cache").mkdir(parents=True)
    (tenants_root / "111" / "cache" / "main.db").write_bytes(b"tenant 111 session")
    # тенант без cache (ещё не логинился) — не должен падать
    (tenants_root / "222").mkdir(parents=True)

    backups_root = tmp_path / "session_backups"
    errors = []

    created = sb.backup_all_sessions(
        backups_root=str(backups_root),
        passphrase="pw",
        tenants_root=str(tenants_root),
        admin_cache_dir=str(admin_cache),
        keep=3,
        on_error=lambda name, e: errors.append((name, e)),
    )

    assert errors == [], f"не должно быть ошибок: {errors}"
    assert len(created) == 2, f"ожидали 2 бэкапа (admin + tenant-111), получили {created}"
    names = {os.path.basename(p).split("-2")[0] for p in created}  # обрезаем по началу timestamp "-20..."
    assert names == {"admin", "tenant-111"}


def test_backup_all_sessions_prunes_old_backups(tmp_path):
    admin_cache = tmp_path / "cache"
    admin_cache.mkdir()
    (admin_cache / "main.db").write_bytes(b"v1")

    backups_root = tmp_path / "session_backups"
    tenants_root = tmp_path / "tenants"
    tenants_root.mkdir()

    import time
    for i in range(5):
        sb.backup_all_sessions(
            backups_root=str(backups_root), passphrase="pw", tenants_root=str(tenants_root),
            admin_cache_dir=str(admin_cache), keep=2,
        )
        time.sleep(1.1)  # у имени файла разрешение в секундах — нужен реальный сдвиг timestamp'а

    remaining = sorted(f for f in os.listdir(backups_root) if f.startswith("admin-"))
    assert len(remaining) == 2, f"должно остаться только 2 последних бэкапа: {remaining}"
