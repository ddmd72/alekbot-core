"""The paid drive evals' hard cost stop (DRIVE_SPECIALIST_RFC §12; memory feedback_no_live_runs_without_ok): G3 has
`--max-usd`, required, from the step-0 "before" run on. No model is called here."""
from types import SimpleNamespace

import pytest

from src.agents.base_agent import BaseAgent
from scripts.validation.drive_eval import smart_dry_run
from scripts.validation.drive_eval.cost_meter import (
    UNPRICED_USD_PER_M, CostCapReached, Meter, estimate_next, price, stop_reason)


def _usage(prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, cache_read_tokens=0,
                           cache_creation_tokens=0)


def test_a_call_once_the_spend_reached_the_cap_raises_and_marks_the_meter_capped():
    meter = Meter(max_usd=0.01)
    meter.record("smart_response", "claude-sonnet-5-5", _usage(5_000, 0), 1.0)   # $0.01 at $2 per M input
    with pytest.raises(CostCapReached):
        meter.check()
    assert meter.capped


def test_no_case_starts_once_the_spend_plus_the_dearest_case_so_far_would_pass_the_cap():
    meter = Meter(max_usd=1.0)
    meter.spent = 0.5
    assert stop_reason(meter, [0.2, 0.4]) == ""                     # 0.5 + 0.4 ≤ 1.0
    meter.spent = 0.7
    assert "would pass --max-usd 1.0" in stop_reason(meter, [0.2, 0.4])   # 0.7 + 0.4 > 1.0
    assert estimate_next([]) == 0.0
    meter.capped = True
    assert "inside a case" in stop_reason(meter, [])


def test_an_unpriced_model_is_charged_at_the_dearer_rate_and_a_priced_one_at_billing():
    usd, priced = price("model-nobody-priced", 1_000_000, 1_000_000, 0, 0)
    assert (usd, priced) == (UNPRICED_USD_PER_M["input"] + UNPRICED_USD_PER_M["output"], False)
    usd, priced = price("claude-sonnet-5-5", 1_000_000, 0, 0, 0)
    assert priced and usd == pytest.approx(2.0)


async def test_the_meter_counts_every_agents_calls_and_its_uninstall_restores_the_class(monkeypatch):
    async def fake_call_llm(agent, request, turn=0, **kwargs):
        return SimpleNamespace(usage_metadata=_usage(1_000_000, 0))

    monkeypatch.setattr(BaseAgent, "_call_llm", fake_call_llm)
    meter = Meter(max_usd=1.5)
    uninstall = meter.install()
    agent = SimpleNamespace(config=SimpleNamespace(agent_type="drive"))
    request = SimpleNamespace(model_name="claude-sonnet-5-5")
    await BaseAgent._call_llm(agent, request)            # $2.00: the spend now passes the cap
    with pytest.raises(CostCapReached):
        await BaseAgent._call_llm(agent, request)        # never reaches the model
    uninstall()
    assert BaseAgent._call_llm is fake_call_llm
    assert [c["agent_type"] for c in meter.calls] == ["drive"]
    assert meter.usd_since(0) == pytest.approx(2.0)


def test_g3_needs_a_max_usd():
    with pytest.raises(SystemExit):
        smart_dry_run._args([])
    assert smart_dry_run._args(["--max-usd", "3"]).max_usd == 3.0


def test_cache_reads_and_writes_are_priced_at_their_own_rates():
    """A cached prefix costs billing.py's cache-read and cache-write legs (calculate_cost), never nothing."""
    from src.domain.billing import calculate_cost
    read, _ = price("claude-sonnet-5-5", 0, 0, 1_000_000, 0)
    written, _ = price("claude-sonnet-5-5", 0, 0, 0, 1_000_000)
    assert read == pytest.approx(calculate_cost("claude-sonnet-5-5", 0, 0, cache_read_tokens=1_000_000)) and read > 0
    assert written == pytest.approx(calculate_cost("claude-sonnet-5-5", 0, 0, cache_creation_tokens=1_000_000))
    assert written > read
