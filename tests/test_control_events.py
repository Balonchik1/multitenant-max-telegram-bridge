import relay_max_to_tg as r


def test_title_change_includes_new_title():
    text = r._format_title_change("Иван Петров", "Новое название")
    assert "Иван Петров" in text
    assert "«Новое название»" in text
    assert "изменил(а) название группы" in text


def test_title_change_without_title_still_reports_rename():
    text = r._format_title_change("Иван Петров", None)
    assert "изменил(а) название группы" in text
    assert "«" not in text


def test_title_change_escapes_html():
    text = r._format_title_change("<b>x</b>", "<script>&")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text
