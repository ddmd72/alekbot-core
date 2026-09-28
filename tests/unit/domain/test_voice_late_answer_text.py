from src.domain.voice_call_note import CALL_NOTE_PREFIX, late_answer_text


def test_late_answer_is_headed_by_the_query_part_of_the_label():
    assert late_answer_text("ask_alek: plan my week", "Monday: gym") == f"{CALL_NOTE_PREFIX}plan my week\n\nMonday: gym"


def test_label_without_a_separator_is_used_whole():
    assert late_answer_text("ask_alek", "done") == f"{CALL_NOTE_PREFIX}ask_alek\n\ndone"
