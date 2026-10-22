"""
The cost stop of the drive evals that run real models (DRIVE_SPECIALIST_RFC §12): G3 (`smart_dry_run`) and the drive
specialist eval (`specialist_ab`, its replay). Every LLM call through BaseAgent._call_llm is metered (who, model,
tokens, $, seconds); a call that would start once the spend has reached `--max-usd` raises CostCapReached, and the
case loop starts no case once the spend so far plus the dearest case so far would pass the cap. Memory
`feedback_no_live_runs_without_ok`: a paid run has a hard stop, not a prose instruction.

An unpriced model is charged at UNPRICED_USD_PER_M, dearer than every model in billing.py, so the stop errs early.

    meter = Meter(max_usd=args.max_usd)
    uninstall = meter.install()
    ...
    if meter.spent + estimate_next(case_costs) > meter.max_usd: stop
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence, Tuple

from src.agents.base_agent import BaseAgent
from src.domain.billing import _PRICING_PER_MILLION_TOKENS, calculate_cost

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
        self.spent += usd
        self.calls.append({"agent_type": agent_type, "model": model, "input": prompt + written, "cached": cached,
                           "output": completion, "usd": usd, "priced": priced, "s": round(seconds, 2)})

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
