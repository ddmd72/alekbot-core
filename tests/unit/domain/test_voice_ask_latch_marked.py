"""`AskLatch.marked(item_id)`: the live text marks the caller's turn the "Ask Lelik" note marked, while the latch holds
it (active or awaiting the reply), and never another turn (VOICE_CALL_LIVE_TEXT_RFC §4.7)."""
from src.domain.voice_ask_latch import AskLatch, AskState


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _latch() -> AskLatch:
    return AskLatch(clock=_Clock(), translate_unmarked=True)


def test_the_marked_turn_is_marked_while_it_is_spoken():
    latch = _latch()
    latch.arm()
    latch.speech_started("item-u7")
    assert latch.state is AskState.ACTIVE
    assert latch.marked("item-u7")


def test_the_marked_turn_stays_marked_while_the_reply_is_awaited_and_played():
    latch = _latch()
    latch.arm()
    latch.speech_started("item-u7")
    latch.speech_stopped("item-u7")
    assert latch.state is AskState.AWAITING_REPLY
    assert latch.marked("item-u7")
    latch.response_created()
    latch.reply_audio()
    assert latch.marked("item-u7")


def test_both_parts_of_a_split_question_are_marked_while_held():
    latch = _latch()
    latch.arm()
    latch.speech_started("item-u7")
    latch.speech_stopped("item-u7")
    assert latch.marked("item-u7")
    latch.speech_started("item-u8")  # a pause split the question before any answer was heard
    assert latch.marked("item-u8")
    latch.speech_stopped("item-u8")
    assert latch.marked("item-u8")


def test_another_turn_and_none_are_not_marked():
    latch = _latch()
    latch.arm()
    latch.speech_started("item-u7")
    assert not latch.marked("item-u9")
    assert not latch.marked(None)


def test_a_marked_none_item_is_still_not_marked():
    latch = _latch()
    latch.arm()
    latch.speech_started(None)
    assert latch.state is AskState.ACTIVE
    assert not latch.marked(None)


def test_nothing_is_marked_before_any_note_or_while_armed():
    latch = _latch()
    assert not latch.marked("item-u7")
    latch.arm()
    assert not latch.marked("item-u7")


def test_the_turn_is_no_longer_marked_after_the_release():
    latch = _latch()
    latch.arm()
    latch.speech_started("item-u7")
    latch.speech_stopped("item-u7")
    latch.response_created()
    latch.response_done()
    assert latch.state is AskState.IDLE
    assert latch.item_id == "item-u7"
    assert not latch.marked("item-u7")


def test_speech_over_the_audible_answer_is_not_marked():
    latch = _latch()
    latch.arm()
    latch.speech_started("item-u7")
    latch.speech_stopped("item-u7")
    latch.response_created()
    latch.reply_audio()
    latch.speech_started("item-u8")  # the caller talks over Lelik's answer
    assert not latch.marked("item-u8")
    latch.speech_stopped("item-u8")
    assert not latch.marked("item-u8")


def test_marked_changes_no_state():
    latch = _latch()
    latch.arm()
    latch.speech_started("item-u7")
    latch.marked("item-u7")
    latch.marked("item-u9")
    assert latch.state is AskState.ACTIVE
    assert latch.item_id == "item-u7"
