import db_meta
import db_messages


async def test_ping_and_mention_isolation_between_tenants(tmp_path):
    """Каждый тенант читает свою собственную БД (ping_map/mention_map) —
    админский пинг/mention-линк не должен быть виден и не должен
    применяться в чужой (тенантской) базе, и наоборот."""
    admin_db = str(tmp_path / "admin.db")
    tenant_db = str(tmp_path / "tenant.db")

    await db_messages.init_db(db_path=admin_db)
    await db_messages.init_db(db_path=tenant_db)

    # Админ ставит пинг и mention-линк в СВОЕЙ базе
    await db_meta.set_ping("админ_слово", "admin_nick", 111, db_path=admin_db)
    await db_meta.set_mention_link(999, "admin_linked", 222, db_path=admin_db)

    # Тенант ничего не ставил — его база должна быть пустой
    tenant_pings = await db_meta.list_pings(db_path=tenant_db)
    assert tenant_pings == [], f"у тенанта не должно быть пингов админа: {tenant_pings}"

    tenant_link = await db_meta.get_mention_link(999, db_path=tenant_db)
    assert tenant_link is None, f"у тенанта не должно быть mention-линков админа: {tenant_link}"

    # apply_pings с db_path тенанта не должен подставлять админский пинг
    text = await db_messages.apply_pings("привет админ_слово мир", db_path=tenant_db)
    assert "админ_слово" in text, f"пинг админа не должен был примениться у тенанта: {text!r}"

    # А с db_path админа — должен
    text2 = await db_messages.apply_pings("привет админ_слово мир", db_path=admin_db)
    assert "tg://user?id=111" in text2, f"пинг админа должен был примениться (по tg_id): {text2!r}"

    # Тенант ставит СВОЙ mention-линк — не должен влиять на админскую базу
    await db_meta.set_mention_link(999, "tenant_linked", 333, db_path=tenant_db)
    admin_link_after = await db_meta.get_mention_link(999, db_path=admin_db)
    assert admin_link_after == ("admin_linked", 222), f"тенант не должен был перезаписать админскую запись: {admin_link_after}"

    tenant_link_after = await db_meta.get_mention_link(999, db_path=tenant_db)
    assert tenant_link_after == ("tenant_linked", 333), f"тенантская запись не сохранилась: {tenant_link_after}"
