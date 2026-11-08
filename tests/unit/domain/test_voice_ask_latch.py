"""AskLatch, row by row from the latch table of VOICE_CALL_CONTROL_CHANNEL_RFC §5.3, plus the two readings its module
docstring decides (the interrupted reply's response_done, speech over an audible answer) and the command echoes."""
import pytest

from src.domain.voice_ask_latch import (
    ASK_ARMED_TIMEOUT_S,
    ASK_REPLY_TIMEOUT_S,
    ASK_RUNAWAY_REPLY_S,
    AskLatch,
    AskReason,
    AskReport,
    AskState,
    AskStep,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def latch(clock: Clock) -> AskLatch:
    return AskLatch(clock)


def _armed(latch: AskLatch) -> AskLatch:
    latch.arm(1)
    return latch


def _active(latch: AskLatch, item: str = "item-u1") -> AskLatch:
    _armed(latch).speech_started(item)
    return latch


def _awaiting(latch: AskLatch, item: str = "item-u1") -> AskLatch:
    _active(latch, item).speech_stopped(item)
    return latch


def test_the_owner_timeouts_are_the_defaults():
    assert ASK_ARMED_TIMEOUT_S == 60.0 and ASK_REPLY_TIMEOUT_S == 20.0


def test_the_page_sees_awaiting_reply_as_active():
    assert [state.wire for state in AskState] == ["idle", "armed", "active", "active"]


# ---------------------------------------------------------------- idle | arm -> armed

def test_idle_arm_arms_and_answers_the_seq(latch):
    assert latch.arm(7) == AskStep(report=AskReport("armed", seq=7))
    assert latch.state is AskState.ARMED


# ---------------------------------------------------------------- armed | disarm -> idle, cancelled

def test_armed_disarm_cancels_with_nothing_to_delete(latch):
    _armed(latch)
    assert latch.disarm(8) == AskStep(insert_note=False, report=AskReport("idle", reason=AskReason.CANCELLED, seq=8))
    assert latch.state is AskState.IDLE


def test_no_note_after_a_cancel(latch):
    _armed(latch).disarm(2)
    assert latch.speech_started("item-u1") == AskStep()
    assert latch.state is AskState.IDLE


# ---------------------------------------------------------------- armed | speech_started -> note, active

def test_armed_speech_started_inserts_the_note_and_remembers_the_item(latch):
    _armed(latch)
    assert latch.speech_started("item-u1") == AskStep(insert_note=True, report=AskReport("active"))
    assert latch.state is AskState.ACTIVE and latch.item_id == "item-u1"


# ---------------------------------------------------------------- armed | 60 s with no speech -> idle, timeout

def test_armed_releases_after_sixty_seconds_without_speech(latch, clock):
    _armed(latch)
    clock.now += 59.9
    assert latch.tick() == AskStep()
    clock.now += 0.1
    assert latch.tick() == AskStep(report=AskReport("idle", reason=AskReason.TIMEOUT))
    assert latch.state is AskState.IDLE


def test_the_armed_timeout_counts_from_the_press(latch, clock):
    clock.now += 50
    _armed(latch)
    clock.now += 50
    assert latch.tick() == AskStep()


# ---------------------------------------------------------------- active | speech_stopped -> awaiting_reply

def test_active_speech_stopped_awaits_the_reply_without_telling_the_page(latch):
    _active(latch)
    assert latch.speech_stopped("item-u1") == AskStep()
    assert latch.state is AskState.AWAITING_REPLY


def test_the_end_of_some_other_speech_does_not_end_the_marked_one(latch):
    _active(latch, "item-u1")
    latch.speech_stopped("item-u9")
    assert latch.state is AskState.ACTIVE


# ---------------------------------------------------------------- active | speech_started -> note again

def test_active_speech_started_inserts_the_note_again(latch):
    _active(latch, "item-u1")
    assert latch.speech_started("item-u2") == AskStep(insert_note=True)
    assert latch.state is AskState.ACTIVE and latch.item_id == "item-u2"


def test_a_pause_that_split_the_question_marks_the_rest_too(latch):
    _awaiting(latch, "item-u1")
    latch.response_created()  # xAI starts a reply on the pause, then stops it when the caller goes on
    assert latch.speech_started("item-u2") == AskStep(insert_note=True)
    assert latch.state is AskState.ACTIVE and latch.item_id == "item-u2"


# ---------------------------------------------------------------- awaiting_reply | response_created / response_done

def test_the_reply_to_the_marked_words_releases_as_answered(latch):
    _awaiting(latch)
    assert latch.response_created() == AskStep()
    assert latch.state is AskState.AWAITING_REPLY
    assert latch.response_done() == AskStep(report=AskReport("idle", reason=AskReason.ANSWERED))
    assert latch.state is AskState.IDLE


def test_the_interrupted_replys_response_done_does_not_release(latch):
    # Lelik was mid-translation when the caller spoke: that reply's response_done arrives before speech_stopped.
    _armed(latch)
    latch.response_created()
    latch.speech_started("item-u1")
    assert latch.response_done() == AskStep()
    latch.speech_stopped("item-u1")
    assert latch.state is AskState.AWAITING_REPLY
    latch.response_created()
    assert latch.response_done().report == AskReport("idle", reason=AskReason.ANSWERED)


def test_a_response_done_after_speech_stopped_but_before_the_reply_started_does_not_release(latch):
    _awaiting(latch)
    assert latch.response_done() == AskStep()
    assert latch.state is AskState.AWAITING_REPLY


def test_speech_over_the_audible_answer_is_not_marked_and_the_answer_still_releases(latch):
    _awaiting(latch)
    latch.response_created()
    latch.reply_audio()
    assert latch.speech_started("item-u2") == AskStep()
    assert latch.state is AskState.AWAITING_REPLY
    assert latch.response_done().report == AskReport("idle", reason=AskReason.ANSWERED)


# ---------------------------------------------------------------- reply never starts (20 s) or never ends (90 s runaway cap) -> timeout

def test_awaiting_reply_times_out_twenty_seconds_after_speech_stopped(latch, clock):
    _awaiting(latch)
    clock.now += 19.9
    assert latch.tick() == AskStep()
    clock.now += 0.1
    assert latch.tick() == AskStep(report=AskReport("idle", reason=AskReason.TIMEOUT))
    assert latch.state is AskState.IDLE


def test_a_started_reply_that_never_ends_times_out_too(latch, clock):
    _awaiting(latch)
    latch.response_created()
    clock.now += ASK_RUNAWAY_REPLY_S
    assert latch.tick().report == AskReport("idle", reason=AskReason.TIMEOUT)


def test_while_the_marked_words_are_spoken_nothing_times_out(latch, clock):
    _active(latch)
    clock.now += 600
    assert latch.tick() == AskStep()
    assert latch.state is AskState.ACTIVE


def test_the_reply_timeout_counts_from_the_last_speech_stopped(latch, clock):
    _awaiting(latch, "item-u1")
    clock.now += 15
    latch.speech_started("item-u2")
    latch.speech_stopped("item-u2")
    clock.now += 15
    assert latch.tick() == AskStep()


# ---------------------------------------------------------------- any | call ends -> call_ended

@pytest.mark.parametrize("reach", [_armed, _active, _awaiting])
def test_the_call_ending_releases_any_latch_that_is_not_idle(latch, reach):
    reach(latch)
    assert latch.end() == AskStep(report=AskReport("idle", reason=AskReason.CALL_ENDED))
    assert latch.state is AskState.IDLE


def test_the_call_ending_on_an_idle_latch_reports_nothing(latch):
    assert latch.end() == AskStep()


# ---------------------------------------------------------------- commands that change nothing echo the state

def test_disarm_while_lelik_is_answering_is_ignored_and_shows_busy(latch):
    _awaiting(latch)
    assert latch.disarm(5) == AskStep(report=AskReport("active", seq=5))
    assert latch.state is AskState.AWAITING_REPLY


def test_a_second_arm_and_a_stray_disarm_echo_the_current_state(latch):
    assert latch.disarm(1) == AskStep(report=AskReport("idle", seq=1))
    latch.arm(2)
    assert latch.arm(3) == AskStep(report=AskReport("armed", seq=3))
    assert latch.state is AskState.ARMED


# ---------------------------------------------------------------- idle ignores the provider

def test_an_idle_latch_ignores_speech_and_replies(latch):
    for step in (latch.speech_started("item-u1"), latch.speech_stopped("item-u1"), latch.response_created(),
                 latch.reply_audio(), latch.response_done(), latch.tick()):
        assert step == AskStep()
    assert latch.state is AskState.IDLE
