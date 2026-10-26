"""The empty-turn rule and the empty-answer alert text (S4 review D-M6/X1/X2)."""
from src.domain.agent import AgentStatus
from src.domain.notify_result import EMPTY_ANSWER_ERROR, NotifyResult, empty_answer_alert, is_empty_answer, is_empty_turn


def test_an_empty_turn_has_no_text_no_rich_content_no_items_and_no_async_dispatch():
    assert is_empty_turn("  ", None, [], [])
    assert is_empty_turn(None, None, (), ())
    assert not is_empty_turn("hi", None, [], [])
    assert not is_empty_turn("", {"rows": []}, [], [])
    assert not is_empty_turn("", None, ["item"], [])
    assert not is_empty_turn("", None, [], ["search_web"])


def test_the_empty_answer_error_is_what_is_empty_answer_reads():
    result = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error=EMPTY_ANSWER_ERROR)
    assert is_empty_answer(result)


def test_the_alert_names_the_path_the_first_eight_characters_of_the_user_and_the_note():
    alert = empty_answer_alert("execute_reminder", "user-1234567890", "note-7")
    assert alert == ("⚠️ Background turn ended with an empty answer (final, not retried). "
                     "path=execute_reminder user=user-123 note=note-7")
    assert "note=" not in empty_answer_alert("drive_reconnect_notice", "user-1234567890")
