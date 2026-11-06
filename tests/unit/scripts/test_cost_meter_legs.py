"""The cost meter records input / cache-read / cache-write apart and prices each leg through billing.py; a nonzero
token count that prices at $0.00 is flagged loudly. No model is called here."""
from types import SimpleNamespace

import pytest

from scripts.validation.drive_eval import cost_meter
from scripts.validation.drive_eval.cost_meter import Meter, format_call


def _usage(prompt=0, completion=0, cache_read=0, cache_write=0):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, cache_read_tokens=cache_read,
                           cache_creation_tokens=cache_write)


def test_three_input_legs_are_recorded_apart_and_each_is_priced_by_billing():
    meter = Meter()
    # claude-sonnet-5-5: $2.00 input, $10.00 output per M, cache read 0.05x, cache write 1.25x of input
    meter.record("smart", "claude-sonnet-5-5", _usage(100_000, 10_000, 1_000_000, 200_000), 1.0)
    call = meter.calls[0]
    assert (call["input"], call["cache_read"], call["cache_write"], call["output"]) == (100_000, 1_000_000, 200_000, 10_000)
    assert call["usd_input"] == pytest.approx(0.20)
    assert call["usd_cache_read"] == pytest.approx(0.10)
    assert call["usd_cache_write"] == pytest.approx(0.50)
    assert call["usd_output"] == pytest.approx(0.10)
    assert call["usd"] == pytest.approx(0.90) and meter.spent == pytest.approx(0.90)
    assert call["zero_cost_legs"] == []
    line = format_call(call)
    assert "input 100000 tok $0.2000" in line and "cache-write 200000 tok $0.5000" in line


def test_cache_write_is_not_folded_into_input():
    meter = Meter()
    meter.record("smart", "claude-sonnet-5-5", _usage(prompt=1_000, cache_write=9_000), 0.1)
    assert meter.calls[0]["input"] == 1_000 and meter.calls[0]["cache_write"] == 9_000


def test_a_leg_with_tokens_that_prices_at_zero_is_flagged_loudly(monkeypatch, capsys):
    monkeypatch.setitem(cost_meter._PRICING_PER_MILLION_TOKENS, "model-without-cache-price",
                        {"input": 2.0, "output": 10.0})
    meter = Meter()
    meter.record("smart", "model-without-cache-price", _usage(prompt=1_000_000, cache_write=1_000_000), 0.1)
    assert meter.calls[0]["zero_cost_legs"] == ["cache_write"]
    assert "COST METER BUG" in capsys.readouterr().err


def test_a_priced_model_with_all_prices_zero_is_flagged_as_a_total_bug(monkeypatch, capsys):
    monkeypatch.setitem(cost_meter._PRICING_PER_MILLION_TOKENS, "free-looking-model", {"input": 0.0, "output": 0.0})
    meter = Meter()
    meter.record("smart", "free-looking-model", _usage(prompt=5, completion=5), 0.1)
    assert meter.calls[0]["zero_cost_legs"] and "COST METER BUG" in capsys.readouterr().err


def test_a_leg_too_small_to_price_above_six_decimals_is_not_flagged():
    from scripts.validation.drive_eval.cost_meter import zero_cost_legs
    assert zero_cost_legs({"input": 3, "output": 2000}, {"input": 0.0, "output": 0.01}) == []
    assert zero_cost_legs({"input": 1000}, {"input": 0.0}) == ["input"]
