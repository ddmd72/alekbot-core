"""The thinking cue must be real G.711 μ-law — Twilio decodes it that way. A home-made
companding curve once shipped: its "silence" bytes (0x7F/0x80) are near full scale in G.711,
so the caller heard a pulsing whistle instead of a breath (live, 2026-09-24)."""
from relay_main import _load_thinking_cue


def _g711_mulaw_decode(byte: int) -> int:
    """ITU-T G.711 μ-law byte → 14-bit linear sample (reference expansion)."""
    byte = ~byte & 0xFF
    magnitude = (((byte & 0x0F) << 3) + 0x84) << ((byte >> 4) & 0x07)
    magnitude -= 0x84
    return -magnitude if byte & 0x80 else magnitude


def test_cue_gap_decodes_to_silence_and_puffs_stay_well_below_full_scale():
    samples = [_g711_mulaw_decode(b) for b in _load_thinking_cue()]

    # The last second of the 2 s loop is the pause between puff pairs.
    assert max(abs(s) for s in samples[-8000:]) < 100
    # Puffs are quiet filler: far from the ±32124 G.711 ceiling.
    assert 500 < max(abs(s) for s in samples) < 12000
