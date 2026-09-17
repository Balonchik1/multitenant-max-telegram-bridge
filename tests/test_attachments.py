from pymax.types.domain.attachments.poll import PollAttachment
from pymax.types.domain.attachments.contact import ContactAttachment
from pymax.types.domain.attachments.call import CallAttachment

import relay_max_to_tg as r


def test_render_poll_text_includes_title_and_answers():
    poll = PollAttachment.model_validate({
        "_type": "POLL",
        "title": "Го в кино?",
        "answers": [{"text": "Да", "answerId": 1}, {"text": "Нет", "answerId": 2}],
        "settings": 0,
        "pollId": 555,
        "version": 1,
        "state": {"total": 0, "voterPreviewIds": []},
    })
    out = r._render_poll_text(poll)
    assert "Го в кино?" in out and "Да" in out and "Нет" in out


def test_render_contact_text_prefers_name_field():
    c = ContactAttachment.model_validate({"_type": "CONTACT", "name": "Иван Иванов"})
    assert r._render_contact_text(c) == "📇 <b>Контакт:</b> Иван Иванов"


def test_render_contact_text_falls_back_to_first_last_name():
    c = ContactAttachment.model_validate({"_type": "CONTACT", "firstName": "Пётр", "lastName": "Петров"})
    assert r._render_contact_text(c) == "📇 <b>Контакт:</b> Пётр Петров"


def test_render_contact_text_handles_completely_empty_attachment():
    c = ContactAttachment.model_validate({"_type": "CONTACT"})
    assert r._render_contact_text(c) == "📇 <b>Контакт:</b> Контакт"


def test_render_call_text_missed():
    call = CallAttachment.model_validate({"_type": "CALL", "hangupType": "MISSED", "callType": "AUDIO"})
    assert r._render_call_text(call) == "📞 Звонок — пропущен"


def test_render_call_text_video_with_duration():
    call = CallAttachment.model_validate({"_type": "CALL", "callType": "VIDEO", "duration": 125})
    assert r._render_call_text(call) == "🎥 Видеозвонок — 2:05"


def test_render_call_text_bare_attachment():
    call = CallAttachment.model_validate({"_type": "CALL"})
    assert r._render_call_text(call) == "📞 Звонок"


def test_strip_html_tags_removes_tags_but_keeps_text():
    assert r._strip_html_tags("📊 <b>Опрос:</b> тест\n<i>сноска</i>") == "📊 Опрос: тест\nсноска"
