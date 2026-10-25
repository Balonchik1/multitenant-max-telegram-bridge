import relay_tg_to_max as r


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.is_forum = True
        self.type = "supergroup"


class FakeMessage:
    def __init__(self, chat_id, text="привет"):
        self.chat = FakeChat(chat_id)
        self.text = text
        self.caption = None
        self.media_group_id = None
        self.message_thread_id = None
        self.message_id = 10
        self.from_user = None
        self.sender_chat = None
        for attr in ("photo", "voice", "audio", "video", "video_note", "sticker", "document",
                     "forward_from", "forward_origin", "reply_to_message"):
            setattr(self, attr, None)


class RecordingClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)

        class _Sent:
            id = 1

        return _Sent()


class FakeExtension:
    def __init__(self, target):
        self.target = target
        self.calls = []

    async def flat_target(self, message, default_max_chat_id, db_path):
        self.calls.append((message, default_max_chat_id, db_path))
        return self.target


def _setup(monkeypatch):
    client = RecordingClient()
    monkeypatch.setattr(r, "_resolve_route", lambda chat_id: (client, "unused.db"))

    async def save_nothing(**kwargs):
        return None

    monkeypatch.setattr(r, "save_message_mapping", save_nothing)
    return client


async def test_flat_group_message_goes_where_the_extension_says(monkeypatch):
    client = _setup(monkeypatch)
    extension = FakeExtension(target=555)
    monkeypatch.setattr(r, "local_ext", extension)

    message = FakeMessage(r.TG_GROUP_FLAT)
    await r.telegram_to_max(message)

    assert client.sent[0]["chat_id"] == 555
    assert extension.calls == [(message, r.FLAT_MAX_CHAT_ID, "unused.db")]


async def test_flat_group_without_extension_uses_the_configured_chat(monkeypatch):
    client = _setup(monkeypatch)
    monkeypatch.setattr(r, "local_ext", None)

    await r.telegram_to_max(FakeMessage(r.TG_GROUP_FLAT))

    assert client.sent[0]["chat_id"] == r.FLAT_MAX_CHAT_ID
