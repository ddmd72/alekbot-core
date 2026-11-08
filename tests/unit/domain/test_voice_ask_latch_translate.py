"""AskLatch translate notes (`translate_unmarked=True`, interpreter calls; owner 2026-11-08): every `speech_started`
the latch does not mark for Lelik asks for the translate note; one step never asks for both notes; one translate note
per utterance."""
from src.domain.voice_ask_latch import AskLatch, AskStep

_TRANSLATE = AskStep(insert_translate_note=True)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _latch() -> AskLatch:
    return AskLatch(Clock(), translate_unmarked=True)


def test_unarmed_speech_asks_for_the_translate_note():
    assert _latch().speech_started("item-u1") == _TRANSLATE


def test_a_latch_without_the_switch_asks_for_no_translate_note():
    assert AskLatch(Clock()).speech_started("item-u1") == AskStep()


def test_armed_speech_gets_only_the_ask_note():
    latch = _latch()
    latch.arm(1)
    step = latch.speech_started("item-u1")
    assert step.insert_note is True and step.insert_translate_note is False


def test_a_pause_split_question_gets_the_ask_note_again_never_the_translate_note():
    latch = _latch()
    latch.arm(1)
    latch.speech_started("item-u1")
    latch.speech_stopped("item-u1")
    step = latch.speech_started("item-u2")
    assert step.insert_note is True and step.insert_translate_note is False


def test_a_second_speech_started_for_the_same_item_gets_no_second_note():
    latch = _latch()
    assert latch.speech_started("item-u1") == _TRANSLATE
    assert latch.speech_started("item-u1") == AskStep()
    latch.speech_stopped("item-u1")
    assert latch.speech_started("item-u1") == AskStep()


def test_a_vad_restart_before_the_utterance_stops_gets_no_second_note_even_without_ids():
    latch = _latch()
    assert latch.speech_started(None) == _TRANSLATE
    assert latch.speech_started(None) == AskStep()
    latch.speech_stopped(None)
    assert latch.speech_started(None) == _TRANSLATE


def test_each_new_utterance_gets_its_own_note():
    latch = _latch()
    assert latch.speech_started("item-u1") == _TRANSLATE
    latch.speech_stopped("item-u1")
    assert latch.speech_started("item-u2") == _TRANSLATE


def test_speech_over_lelik_s_audible_answer_is_translated():
    latch = _latch()
    latch.arm(1)
    latch.speech_started("item-u1")
    latch.speech_stopped("item-u1")
    latch.response_created()
    latch.reply_audio()
    assert latch.speech_started("item-u2") == _TRANSLATE


def test_after_a_cancel_speech_is_translated():
    latch = _latch()
    latch.arm(1)
    latch.disarm(2)
    assert latch.speech_started("item-u1") == _TRANSLATE
