"""T-R19: the per-window reservation bounds a window's worst case (Δ106 hard month cap): audio input plus the
per-window output cap, at the held batch price."""
import math

import pytest

from src.domain.billing import BATCH_DISCOUNT, _PRICING_PER_MILLION_TOKENS
from src.domain.drive_index_params import TRANSCRIBE_HOUR_USD_ESTIMATE
from src.infrastructure.agent_config import DRIVE_TRANSCRIBER

AUDIO_TOKENS_PER_MIN = 1_500    # ~25 tokens per second of audio


@pytest.mark.parametrize("seconds", [60, 1_800, 1_860, 2_760])
def test_reservation_covers_the_worst_case_window(seconds):
    rates = _PRICING_PER_MILLION_TOKENS["gemini-3.8-flash"]
    cap = min(DRIVE_TRANSCRIBER.max_tokens, math.ceil(1_000 * seconds / 60))
    input_tokens = AUDIO_TOKENS_PER_MIN * seconds / 60
    worst = (input_tokens * rates["input"] + cap * rates["output"]) / 1e6 * BATCH_DISCOUNT
    assert seconds * TRANSCRIBE_HOUR_USD_ESTIMATE / 3600 >= worst
