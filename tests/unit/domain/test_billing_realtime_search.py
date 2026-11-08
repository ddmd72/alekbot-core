"""xAI's server-side search calls in a voice session are priced per call (docs.x.ai/developers/pricing, 2026-11-08)."""
from src.domain.billing import (
    REALTIME_WEB_SEARCH_CALLS,
    REALTIME_X_SEARCH_CALLS,
    calculate_realtime_cost,
    realtime_token_count,
    unpriced_realtime_legs,
)


def test_web_search_calls_are_five_dollars_per_thousand():
    assert calculate_realtime_cost("grok-voice-3.0", {"web_search_calls": 1000}) == 5.0
    assert calculate_realtime_cost("grok-voice-3.0", {"web_search_calls": 3}) == 0.015


def test_an_x_search_call_is_priced_as_ten_items_at_the_profile_rate():
    # Billed per item fetched; the response carries no item count, so a call is priced as 10 x $10 / 1k.
    assert calculate_realtime_cost("grok-voice-3.0", {"x_search_calls": 1}) == 0.1
    assert calculate_realtime_cost("grok-voice-3.0", {"x_search_calls": 4}) == 0.4


def test_search_calls_add_to_the_audio_and_text_legs():
    usage = {"billable_audio_seconds": 60, "text_input_tokens": 1_000_000, "web_search_calls": 2, "x_search_calls": 1}
    assert calculate_realtime_cost("grok-voice-3.0", usage) == round(0.08 + 0.004 + 0.01 + 0.1, 6)


def test_search_calls_are_not_tokens():
    usage = {"audio_input_tokens": 10, "text_output_tokens": 5, "web_search_calls": 2, "x_search_calls": 1}
    assert realtime_token_count(usage) == 15


def test_the_legs_the_adapter_reports_are_the_legs_billing_prices():
    from src.adapters.xai_realtime_adapter import _BUILTIN_TOOL_LEGS, _MODEL

    assert set(_BUILTIN_TOOL_LEGS.values()) == {REALTIME_WEB_SEARCH_CALLS, REALTIME_X_SEARCH_CALLS}
    for leg in _BUILTIN_TOOL_LEGS.values():
        assert unpriced_realtime_legs(_MODEL, {leg: 1}) == []


def test_xai_adapter_default_model_prices_every_search_leg_guard_against_fail_open_zero_cost():
    """calculate_realtime_cost bills an unpriced leg at $0 without a word. A model bump or a new
    built-in tool without a price would make every search free; this fails first."""
    from src.adapters.xai_realtime_adapter import _BUILTIN_TOOL_LEGS, _MODEL

    for leg in _BUILTIN_TOOL_LEGS.values():
        assert calculate_realtime_cost(_MODEL, {leg: 1}) > 0, leg


def test_an_unpriced_call_leg_is_named():
    assert unpriced_realtime_legs("grok-voice-3.0", {"web_search_calls": 1, "code_execution_calls": 2}) == [
        "code_execution_calls"]
    # A model without per-call prices (the deprecated 2.0, OpenAI) names every non-zero call leg.
    assert unpriced_realtime_legs("grok-voice-think-fast-2.0", {"web_search_calls": 1, "x_search_calls": 0}) == [
        "web_search_calls"]


def test_token_and_second_legs_are_never_reported_as_unpriced_calls():
    usage = {"audio_input_tokens": 100, "billable_audio_seconds": 30, "cached_tokens": 5}
    assert unpriced_realtime_legs("grok-voice-3.0", usage) == []
    assert unpriced_realtime_legs("gpt-realtime-2.1", usage) == []
