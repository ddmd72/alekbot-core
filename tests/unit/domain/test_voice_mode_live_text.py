"""Which voice modes show the call's text live (VOICE_CALL_LIVE_TEXT_RFC §4.1): the two mode pages, not the main one."""
from src.domain.voice_mode import VOICE_MODES_WITH_LIVE_TEXT, VoiceMode


def test_only_the_interpreter_and_the_tutor_show_live_text():
    assert VOICE_MODES_WITH_LIVE_TEXT == frozenset({VoiceMode.INTERPRETER, VoiceMode.TUTOR})
    assert VoiceMode.LELIK not in VOICE_MODES_WITH_LIVE_TEXT
