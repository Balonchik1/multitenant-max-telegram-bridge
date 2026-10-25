import pytest

import db_messages
import relay_max_to_tg as rm

GROUP = -70001
OTHER_GROUP = -70002


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "t.db")
    await db_messages.init_db(db_path=path)
    return path


async def test_title_roundtrip_and_overwrite(db):
    assert await db_messages.get_group_title(GROUP, db_path=db) is None
    await db_messages.set_group_title(GROUP, "Первое", db_path=db)
    assert await db_messages.get_group_title(GROUP, db_path=db) == "Первое"
    await db_messages.set_group_title(GROUP, "Второе", db_path=db)
    assert await db_messages.get_group_title(GROUP, db_path=db) == "Второе"


async def test_only_if_missing_keeps_the_known_title(db):
    await db_messages.set_group_title(GROUP, "Известное", db_path=db)
    await db_messages.set_group_title(GROUP, "Другое", db_path=db, only_if_missing=True)
    await db_messages.set_group_title(OTHER_GROUP, "Новое", db_path=db, only_if_missing=True)
    assert await db_messages.get_group_title(GROUP, db_path=db) == "Известное"
    assert await db_messages.get_group_title(OTHER_GROUP, db_path=db) == "Новое"


def test_message_shows_old_and_new_title():
    text = rm._format_title_change("Сергей", "Новое", "Старое")
    assert "«Старое» → «Новое»" in text


def test_message_shows_only_new_title_when_old_is_unknown_or_same():
    assert "→" not in rm._format_title_change("Сергей", "Новое", None)
    assert "→" not in rm._format_title_change("Сергей", "Новое", "Новое")
    assert "«Новое»" in rm._format_title_change("Сергей", "Новое")


def test_message_without_new_title_still_says_it_changed():
    assert "изменил(а) название группы" in rm._format_title_change("Сергей", None, "Старое")
    assert "«" not in rm._format_title_change("Сергей", None, "Старое")


def test_titles_are_html_escaped():
    text = rm._format_title_change("<b>Х</b>", "A & B", "<i>C</i>")
    assert "&lt;i&gt;C&lt;/i&gt;" in text and "A &amp; B" in text and "&lt;b&gt;Х&lt;/b&gt;" in text


async def test_first_rename_with_unknown_old_title_then_chain(db):
    first = await rm._describe_title_change(GROUP, "Сергей", "Первое", db_path=db)
    assert "→" not in first and "«Первое»" in first

    second = await rm._describe_title_change(GROUP, "Сергей", "Второе", db_path=db)
    assert "«Первое» → «Второе»" in second
    assert await db_messages.get_group_title(GROUP, db_path=db) == "Второе"


async def test_title_notice_survives_database_failure(tmp_path):
    text = await rm._describe_title_change(GROUP, "Сергей", "Новое", db_path=str(tmp_path / "missing" / "no.db"))
    assert "«Новое»" in text


class FakeChat:
    def __init__(self, chat_id, title):
        self.id = chat_id
        self.title = title


class FakeClient:
    def __init__(self, cached, fetched=None, fetch_fails=False):
        self.chats = cached
        self._fetched = fetched
        self._fetch_fails = fetch_fails

    async def fetch_chats(self):
        if self._fetch_fails:
            raise RuntimeError("Not connected")
        return self._fetched


async def test_seed_saves_only_group_titles_and_keeps_known_ones(db):
    await db_messages.set_group_title(GROUP, "Раньше", db_path=db)
    client = FakeClient(
        cached=[],
        fetched=[
            FakeChat(GROUP, "Сейчас"),          # уже известна: не затираем
            FakeChat(OTHER_GROUP, "Другая"),    # новая группа
            FakeChat(555, "Личный диалог"),     # не группа (id > 0)
            FakeChat(-70003, None),             # группа без названия
        ],
    )
    await rm._seed_group_titles(client, db_path=db)

    assert await db_messages.get_group_title(GROUP, db_path=db) == "Раньше"
    assert await db_messages.get_group_title(OTHER_GROUP, db_path=db) == "Другая"
    assert await db_messages.get_group_title(555, db_path=db) is None
    assert await db_messages.get_group_title(-70003, db_path=db) is None


async def test_seed_falls_back_to_cached_chats_when_fetch_fails(db):
    client = FakeClient(cached=[FakeChat(GROUP, "Из кэша")], fetch_fails=True)
    await rm._seed_group_titles(client, db_path=db)
    assert await db_messages.get_group_title(GROUP, db_path=db) == "Из кэша"


async def test_seed_never_raises(tmp_path):
    client = FakeClient(cached=[FakeChat(GROUP, "X")], fetched=[FakeChat(GROUP, "X")])
    await rm._seed_group_titles(client, db_path=str(tmp_path / "missing" / "no.db"))
