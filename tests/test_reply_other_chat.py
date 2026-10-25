import relay_max_to_tg as rm

TARGET = -1001110009
OTHER = -1001110008


def test_reply_to_message_in_same_chat_is_kept():
    assert rm._reply_tg_id_in_chat((TARGET, 5, 77), TARGET) == 77


def test_reply_to_message_in_another_chat_is_dropped():
    """Чат MAX перенесли в другую TG-группу: ответ на старое сообщение, оставшееся
    в прежней группе, не должен ломать отправку."""
    assert rm._reply_tg_id_in_chat((OTHER, 5, 77), TARGET) is None


def test_unknown_message_gives_no_reply_link():
    assert rm._reply_tg_id_in_chat(None, TARGET) is None


def test_flat_group_message_has_no_thread_but_still_matches_chat():
    assert rm._reply_tg_id_in_chat((TARGET, None, 78), TARGET) == 78
