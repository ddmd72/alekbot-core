"""AskLatch reply timeouts (owner: the button releases when Lelik's answer ends): the 20 s timeout covers only a
reply that never starts; once the reply has started, nothing but its response_done releases the button, up to a
90 s runaway cap counted from the reply's start."""
from src.domain.voice_ask_latch import ASK_RUNAWAY_REPLY_S, AskLatch, AskReason, AskReport, AskState, AskStep


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _awaiting(clock: Clock) -> AskLatch:
    latch = AskLatch(clock)
    latch.arm(1)
    latch.speech_started("item-u1")
    latch.speech_stopped("item-u1")
    return latch


def test_the_runaway_cap_is_ninety_seconds():
    assert ASK_RUNAWAY_REPLY_S == 90.0


def test_a_long_answer_stays_pressed_until_its_response_done():
    clock = Clock()
    latch = _awaiting(clock)
    clock.now += 0.4
    latch.response_created()
    latch.reply_audio()
    for _ in range(80):           # Lelik speaks for 80 s
        clock.now += 1.0
        assert latch.tick() == AskStep()
    assert latch.state is AskState.AWAITING_REPLY
    assert latch.response_done() == AskStep(report=AskReport("idle", reason=AskReason.ANSWERED))


def test_a_reply_that_never_starts_releases_twenty_seconds_after_speech_stopped():
    clock = Clock()
    latch = _awaiting(clock)
    clock.now += 19.9
    assert latch.tick() == AskStep()
    clock.now += 0.1
    assert latch.tick() == AskStep(report=AskReport("idle", reason=AskReason.TIMEOUT))


def test_a_reply_that_starts_late_is_not_cut_by_the_twenty_second_timeout():
    clock = Clock()
    latch = _awaiting(clock)
    clock.now += 15.0
    latch.response_created()
    clock.now += 30.0              # 45 s after speech_stopped, 30 s into the reply
    assert latch.tick() == AskStep()
    assert latch.state is AskState.AWAITING_REPLY


def test_a_runaway_reply_releases_ninety_seconds_after_it_started():
    clock = Clock()
    latch = _awaiting(clock)
    clock.now += 1.0
    latch.response_created()
    clock.now += 89.9
    assert latch.tick() == AskStep()
    clock.now += 0.1
    assert latch.tick() == AskStep(report=AskReport("idle", reason=AskReason.TIMEOUT))
