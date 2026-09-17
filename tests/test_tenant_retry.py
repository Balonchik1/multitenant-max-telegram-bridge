import tenants


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, uid, text):
        self.from_user = FakeUser(uid)
        self.text = text
        self.chat = type("C", (), {"type": "private"})()
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


async def _run_retry(monkeypatch, status: str):
    """Прогоняет tenant_onboarding_input с /retry для данного статуса и
    возвращает список статусов, с которыми был вызван _set_status."""
    set_status_calls = []

    async def fake_get_tenant(owner_id):
        return {"status": status, "max_phone": "79990000000"}

    async def fake_set_status(owner_id, new_status, **fields):
        set_status_calls.append(new_status)

    def fake_create_task(coro):
        coro.close()  # не запускаем реальный _run_login, просто гасим корутину
        return None

    monkeypatch.setattr(tenants, "_get_tenant", fake_get_tenant)
    monkeypatch.setattr(tenants, "_set_status", fake_set_status)
    monkeypatch.setattr(tenants.asyncio, "create_task", fake_create_task)

    msg = FakeMessage(uid=555, text="/retry")
    await tenants.tenant_onboarding_input(msg)
    return set_status_calls


async def test_retry_preserves_active_status(monkeypatch):
    """/retry для уже активного (привязанного) тенанта не должен откатывать
    его в awaiting_login — иначе следующий реконнект теряет "это тихий
    реконнект уже активного тенанта" и пропускает добор пропущенного
    (живой случай: /retry после обрыва связи откатывал active -> awaiting_group)."""
    calls = await _run_retry(monkeypatch, "active")
    assert calls == [], f"/retry не должен менять статус для active: {calls}"


async def test_retry_preserves_awaiting_group_status(monkeypatch):
    calls = await _run_retry(monkeypatch, "awaiting_group")
    assert calls == [], f"/retry не должен менять статус для awaiting_group: {calls}"


async def test_retry_resets_needs_2fa_to_awaiting_login(monkeypatch):
    """needs_2fa/failed — вход не завершился, тут сброс в awaiting_login
    по-прежнему нужен, чтобы _run_login начал попытку входа заново."""
    calls = await _run_retry(monkeypatch, "needs_2fa")
    assert calls == ["awaiting_login"], calls


async def test_retry_resets_failed_to_awaiting_login(monkeypatch):
    calls = await _run_retry(monkeypatch, "failed")
    assert calls == ["awaiting_login"], calls
