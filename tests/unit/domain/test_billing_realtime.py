from src.domain.billing import calculate_realtime_cost


def test_calculate_realtime_cost_prices_all_four_legs():
    usage = {
        "audio_input_tokens": 1_000_000,
        "audio_output_tokens": 1_000_000,
        "text_input_tokens": 1_000_000,
        "text_output_tokens": 1_000_000,
    }
    cost = calculate_realtime_cost("gpt-realtime-2.1", usage)
    # 32 + 64 + 4 + 24 = 124, per RFC §6's rate table
    assert cost == 124.0


def test_calculate_realtime_cost_applies_cached_rate():
    usage = {"cached_tokens": 1_000_000}
    cost = calculate_realtime_cost("gpt-realtime-2.1", usage)
    assert cost == 0.40


def test_calculate_realtime_cost_unknown_model_returns_zero():
    assert calculate_realtime_cost("unknown-model", {"audio_input_tokens": 1_000_000}) == 0.0


def test_calculate_realtime_cost_missing_keys_default_to_zero():
    assert calculate_realtime_cost("gpt-realtime-2.1", {}) == 0.0


def test_calculate_realtime_cost_matches_flattened_real_openai_shape():
    """End-to-end check against the REAL nested OpenAI response.done usage
    shape (verified against developers.openai.com's live reference +
    community-reported payloads, checked 2026-09-21) as it is actually
    flattened by OpenAIRealtimeAdapter._flatten_usage before it ever reaches
    this function — not the brief's idealized already-flat dict.

    Real shape:
        {"input_token_details": {"text_tokens": 120, "audio_tokens": 80,
                                   "cached_tokens": 40,
                                   "cached_tokens_details": {"text_tokens": 40, "audio_tokens": 0}},
         "output_token_details": {"text_tokens": 50, "audio_tokens": 150}}

    cached_tokens (40) is a SUBSET of input_token_details.text_tokens (120),
    not additive, so the adapter subtracts it before flattening:
        audio_input_tokens = 80 - 0   = 80
        text_input_tokens  = 120 - 40 = 80
        audio_output_tokens = 150
        text_output_tokens  = 50
        cached_tokens        = 40

    cost = 80*32/1e6 + 150*64/1e6 + 80*4/1e6 + 50*24/1e6 + 40*0.40/1e6
         = (2560 + 9600 + 320 + 1200 + 16) / 1e6 = 0.013696
    """
    from src.adapters.openai_realtime_adapter import _flatten_usage

    real_nested_usage = {
        "total_tokens": 400,
        "input_tokens": 200,
        "output_tokens": 200,
        "input_token_details": {
            "text_tokens": 120,
            "audio_tokens": 80,
            "image_tokens": 0,
            "cached_tokens": 40,
            "cached_tokens_details": {"text_tokens": 40, "audio_tokens": 0, "image_tokens": 0},
        },
        "output_token_details": {"text_tokens": 50, "audio_tokens": 150},
    }

    flat = _flatten_usage(real_nested_usage)
    cost = calculate_realtime_cost("gpt-realtime-2.1", flat)

    assert flat == {
        "audio_input_tokens": 80,
        "audio_output_tokens": 150,
        "text_input_tokens": 80,
        "text_output_tokens": 50,
        "cached_tokens": 40,
    }
    assert round(cost, 6) == 0.013696
