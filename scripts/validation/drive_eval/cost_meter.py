"""
The cost stop of the drive evals that run real models (DRIVE_SPECIALIST_RFC §12): G3 (`smart_dry_run`) and the drive
specialist eval (`specialist_ab`, its replay). Every LLM call through BaseAgent._call_llm is metered (who, model,
tokens, $, seconds); a call that would start once the spend has reached `--max-usd` raises CostCapReached, and the
case loop starts no case once the spend so far plus the dearest case so far would pass the cap. Memory
`feedback_no_live_runs_without_ok`: a paid run has a hard stop, not a prose instruction.

An unpriced model is charged at UNPRICED_USD_PER_M, dearer than every model in billing.py, so the stop errs early.

Each call records its three input legs apart (`input` = non-cached prompt tokens, `cache_read`, `cache_write`) and the
dollars of each leg (`usd_input`, `usd_cache_read`, `usd_cache_write`, `usd_output`), every leg priced by
billing.py's `calculate_cost` (one leg at a time, so the cache multipliers stay billing's). `cached` stays as an alias of
`cache_read` for older readers of the report. A call with tokens in a leg that costs $0.00 is a pricing bug: it is
flagged (`zero_cost_legs`) and warned about loudly on stderr and in the log.

    meter = Meter(max_usd=args.max_usd)
    uninstall = meter.install()
    ...
    if meter.spent + estimate_next(case_costs) > meter.max_usd: stop
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence, Tuple

from src.agents.base_agent import BaseAgent
from src.domain.billing import _PRICING_PER_MILLION_TOKENS, calculate_cost
from src.utils.logger import logger

# an unpriced model is charged at this rate for the cost stop (dearer than every model in billing.py)
UNPRICED_USD_PER_M = {"input": 5.0, "output": 25.0}


class CostCapReached(RuntimeError):
    """The next LLM call would start past --max-usd."""


def price(model: str, prompt: int, completion: int, cached: int, written: int) -> Tuple[float, bool]:
    """(usd, priced): billing.py's price for a known model, the unpriced rate otherwise."""
    if model in _PRICING_PER_MILLION_TOKENS:
        return calculate_cost(model, prompt, completion, cache_read_tokens=cached, cache_creation_tokens=written), True
    usd = (prompt + cached + written) / 1e6 * UNPRICED_USD_PER_M["input"] + completion / 1e6 * UNPRICED_USD_PER_M["output"]
    return round(usd, 6), False


def leg_prices(model: str, prompt: int, completion: int, cached: int, written: int) -> Dict[str, float]:
    """The dollars of each leg: billing.py's price for a known model (one leg at a time), the unpriced rate otherwise
    (cache legs at the input rate)."""
    if model in _PRICING_PER_MILLION_TOKENS:
        return {
            "input": calculate_cost(model, prompt, 0, 0, 0),
            "cache_read": calculate_cost(model, 0, 0, cached, 0),
            "cache_write": calculate_cost(model, 0, 0, 0, written),
            "output": calculate_cost(model, 0, completion, 0, 0),
        }
    rate_in, rate_out = UNPRICED_USD_PER_M["input"] / 1e6, UNPRICED_USD_PER_M["output"] / 1e6
    return {"input": round(prompt * rate_in, 6), "cache_read": round(cached * rate_in, 6),
            "cache_write": round(written * rate_in, 6), "output": round(completion * rate_out, 6)}


def zero_cost_legs(tokens: Dict[str, int], legs: Dict[str, float]) -> List[str]:
    """The legs that carry tokens and cost nothing: a price missing from billing.py, never a free call."""
    return [leg for leg, n in tokens.items() if n > 0 and legs[leg] <= 0.0]


def format_call(call: Dict[str, Any]) -> str:
    """One line per call: the three input legs with their dollars, the output, the total."""
    return (f"{call['agent_type']} {call['model']}: input {call['input']} tok ${call['usd_input']:.4f} | "
            f"cache-read {call['cache_read']} tok ${call['usd_cache_read']:.4f} | "
            f"cache-write {call['cache_write']} tok ${call['usd_cache_write']:.4f} | "
            f"output {call['output']} tok ${call['usd_output']:.4f} | total ${call['usd']:.4f}"
            + ("" if call["priced"] else " (UNPRICED model: stop-rate estimate)")
            + (f"  !! ZERO COST for {call['zero_cost_legs']}" if call["zero_cost_legs"] else ""))


@dataclass
class Meter:
    """Every LLM call through BaseAgent._call_llm (all agents): who, model, tokens, $, seconds; the cost stop."""
    max_usd: float = float("inf")
    calls: List[Dict[str, Any]] = field(default_factory=list)
    spent: float = 0.0
    capped: bool = False

    def mark(self) -> int:
        return len(self.calls)

    def since(self, mark: int) -> List[Dict[str, Any]]:
        return self.calls[mark:]

    def usd_since(self, mark: int) -> float:
        return round(sum(float(c["usd"]) for c in self.since(mark)), 6)

    def check(self) -> None:
        if self.spent >= self.max_usd:
            self.capped = True
            raise CostCapReached(f"spent ${self.spent:.4f} of --max-usd {self.max_usd}")

    def record(self, agent_type: str, model: str, usage: Any, seconds: float) -> None:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = int(getattr(usage, "cache_read_tokens", 0) or 0)
        written = int(getattr(usage, "cache_creation_tokens", 0) or 0)
        usd, priced = price(model, prompt, completion, cached, written)
        legs = leg_prices(model, prompt, completion, cached, written)
        flagged = zero_cost_legs({"input": prompt, "cache_read": cached, "cache_write": written, "output": completion}, legs)
        if priced and usd <= 0.0 and (prompt or completion or cached or written):
            flagged = flagged or ["total"]
        self.spent += usd
        call = {"agent_type": agent_type, "model": model, "input": prompt, "cache_read": cached, "cache_write": written,
                "cached": cached, "output": completion, "usd": usd, "usd_input": legs["input"],
                "usd_cache_read": legs["cache_read"], "usd_cache_write": legs["cache_write"],
                "usd_output": legs["output"], "priced": priced, "zero_cost_legs": flagged, "s": round(seconds, 2)}
        self.calls.append(call)
        if flagged:
            message = f"COST METER BUG: {format_call(call)}: tokens that price at $0.00; fix billing.py before trusting any $ figure"
            logger.error(message)
            print(message, file=sys.stderr)

    def install(self) -> Callable[[], None]:
        """Meter every agent's LLM calls (eager or lazily built later); returns the uninstall."""
        original = BaseAgent._call_llm
        meter = self

        async def metered(agent: BaseAgent, request: Any, turn: int = 0, **kwargs: Any) -> Any:
            meter.check()
            started = time.monotonic()
            response = await original(agent, request, turn, **kwargs)
            meter.record(agent.config.agent_type, str(request.model_name), getattr(response, "usage_metadata", None),
                         time.monotonic() - started)
            return response

        BaseAgent._call_llm = metered   # type: ignore[method-assign]

        def uninstall() -> None:
            BaseAgent._call_llm = original   # type: ignore[method-assign]
        return uninstall


def estimate_next(case_costs: Sequence[float]) -> float:
    """The next case is estimated at the dearest case so far (0 before the first)."""
    return max((float(c) for c in case_costs), default=0.0)


def stop_reason(meter: Meter, case_costs: Sequence[float]) -> str:
    """Why the next case must not start, or "" when it may."""
    if meter.capped:
        return f"the cost cap was reached inside a case (${meter.spent:.4f} of --max-usd {meter.max_usd})"
    nxt = estimate_next(case_costs)
    if meter.spent + nxt > meter.max_usd:
        return f"${meter.spent:.4f} spent + ${nxt:.4f} for the next case would pass --max-usd {meter.max_usd}"
    return ""
