"""The relay ships the chosen thinking cue (owner pick B2, 2026-09-24): one 2 s loop of μ-law 8 kHz."""
from relay_main import _load_thinking_cue

_MULAW_SILENCE = {0xFF, 0x7F}


def test_thinking_cue_is_one_two_second_mulaw_loop_in_whole_20ms_frames():
    clip = _load_thinking_cue()

    assert len(clip) == 16000
    assert len(clip) % 160 == 0
    assert set(clip) - _MULAW_SILENCE  # audible, not a file of line silence
