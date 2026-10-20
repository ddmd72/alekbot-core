"""Batch pricing for drive transcripts (USER_DRIVE_INDEXING_RFC §27, Δ105/Δ131/Δ132; plan G7): the Gemini Batch API
bills half the interactive rate, one input rate for every modality, thought tokens in the output leg. No price row is
edited: `gemini-3.8-flash` stays at its held final price."""
import hashlib
import json

import pytest

from src.domain.billing import BATCH_DISCOUNT, BatchUsage, _PRICING_PER_MILLION_TOKENS, calculate_cost

MODEL = "gemini-3.8-flash"


def test_audio_and_text_input_priced_alike():
    audio = BatchUsage(MODEL, audio_input_tokens=100_000).cost()
    text = BatchUsage(MODEL, text_input_tokens=100_000).cost()
    split = BatchUsage(MODEL, audio_input_tokens=60_000, text_input_tokens=40_000).cost()
    assert audio == text == split > 0


def test_batch_discount_on_both_legs():
    usage = BatchUsage(MODEL, audio_input_tokens=1_000_000, output_tokens=1_000_000)
    assert usage.cost() == pytest.approx(calculate_cost(MODEL, 1_000_000, 1_000_000) * BATCH_DISCOUNT)
    assert BatchUsage(MODEL, audio_input_tokens=1_000_000).cost() == pytest.approx(0.75)
    assert BatchUsage(MODEL, output_tokens=1_000_000).cost() == pytest.approx(3.75)


def test_thought_tokens_in_output_leg():
    assert BatchUsage(MODEL, thought_tokens=1_000_000).cost() == BatchUsage(MODEL, output_tokens=1_000_000).cost()


def test_unknown_model_batch_cost_is_zero_and_flagged():
    usage = BatchUsage("no-such-model", audio_input_tokens=10_000, output_tokens=1_000)
    assert usage.cost() == 0.0 and not usage.is_priced()
    assert BatchUsage(MODEL).is_priced()


def test_a_thirty_minute_window_prices_as_the_docstring_says():
    cost = BatchUsage(MODEL, audio_input_tokens=45_000, output_tokens=6_000).cost()
    assert cost == pytest.approx(0.05625)
    assert cost * 2 == pytest.approx(0.1125 + 0.0, rel=1e-9)   # two windows an hour, before thought tokens


def test_price_rows_hash_pin():
    """A price edit (or a new discount) fails here and forces a review: G7 holds the final $1.50/$7.50 on purpose."""
    row = json.dumps(_PRICING_PER_MILLION_TOKENS[MODEL], sort_keys=True)
    digest = hashlib.sha256(f"{row}|{BATCH_DISCOUNT!r}".encode()).hexdigest()
    assert digest == "cd7d772ea6046acdd0a493056b568062f66e24811aef820d853412c3fa95d550"
