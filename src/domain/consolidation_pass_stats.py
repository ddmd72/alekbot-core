"""Per-pass statistics of one consolidation loop, and the ops-alert texts built from them.

Pure data and formatting: no I/O, no LLM. Born from the Sonnet 5.5 adaptive-thinking stall
(see docs/04_solution_strategy/decisions/sonnet55_adaptive_thinking_stall.md): a pass that
runs out of turns without a final report must be loud, and while the model choice is being
watched every pass reports a compact stats line.

Privacy: the texts carry only the first 8 characters of the user id and tool NAMES/counts,
never tool arguments or fact content.
"""

import json
from collections import Counter
from typing import Any, Dict, List, Sequence, Tuple

_WRITE_TOOLS = frozenset({"create_fact", "update_fact", "merge_facts"})
_SHORT_NAMES = {
    "search_existing_facts": "search",
    "create_fact": "create",
    "update_fact": "update",
    "merge_facts": "merge",
    "count_words": "count",
}


def stats_alerts_enabled(raw: str) -> bool:
    """Parse the CONSOLIDATION_STATS_ALERTS value: "0", "false", "no", "off" (any case) are off."""
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _short(name: str) -> str:
    return _SHORT_NAMES.get(name, name)


def _signature(calls: Sequence[Tuple[str, Dict[str, Any]]]) -> Tuple[str, ...]:
    """Order-insensitive identity of one turn's tool calls (name + args)."""
    return tuple(sorted(
        f"{name}:{json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)}"
        for name, args in calls
    ))


class ConsolidationPassStats:
    """Collects what one `_run_consolidation_loop` pass did, turn by turn."""

    def __init__(
        self,
        pass_label: str,
        model: str,
        effort: Any,
        max_turns: int,
        user_id: str,
    ) -> None:
        self.pass_label = pass_label
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.user_prefix = (user_id or "")[:8]
        self._turns: List[List[str]] = []
        self._signatures: List[Tuple[str, ...]] = []
        self.repeats = 0

    def record_turn(self, calls: Sequence[Tuple[str, Dict[str, Any]]]) -> None:
        """Record one turn's tool calls as (name, args) pairs."""
        sig = _signature(calls)
        if self._signatures and self._signatures[-1] == sig:
            self.repeats += 1
        self._signatures.append(sig)
        self._turns.append([name for name, _ in calls])

    @property
    def turns_used(self) -> int:
        return len(self._turns)

    @property
    def counts(self) -> Dict[str, int]:
        c: "Counter[str]" = Counter()
        for names in self._turns:
            c.update(names)
        return dict(c)

    @property
    def writes(self) -> int:
        return sum(n for name, n in self.counts.items() if name in _WRITE_TOOLS)

    def sequence(self) -> str:
        """Compact per-turn tool sequence, e.g. `t1 search×3 · t2 create×3`."""
        parts = []
        for i, names in enumerate(self._turns, start=1):
            c = Counter(_short(n) for n in names)
            body = " ".join(f"{n}×{k}" for n, k in c.items()) or "-"
            parts.append(f"t{i} {body}")
        return " · ".join(parts)

    def _counts_text(self) -> str:
        return " ".join(f"{_short(n)}×{k}" for n, k in self.counts.items()) or "none"

    def format_summary(self, report_ops: int, seconds: float) -> str:
        """The temporary per-pass stats line (pass ended with a final report). Turns count every
        LLM call, the final report included, because that is what MAX turns caps."""
        return (
            f"📊 Consolidation pass `{self.pass_label}` | model {self.model} | effort {self.effort} | "
            f"turns {self.turns_used + 1}/{self.max_turns} | tools: {self._counts_text()} | "
            f"repeats {self.repeats} | writes {self.writes} | report ops {report_ops} | "
            f"{seconds:.0f}s | user {self.user_prefix}\n{self.sequence()}"
        )

    def format_limit_alert(self, seconds: float) -> str:
        """The permanent alert: turn cap reached, no final report."""
        return (
            f"🚨 Consolidation hit the turn limit without a final report | pass `{self.pass_label}` | "
            f"model {self.model} | effort {self.effort} | turns {self.turns_used}/{self.max_turns} | "
            f"writes so far {self.writes} | repeats {self.repeats} | {seconds:.0f}s | "
            f"user {self.user_prefix}\n{self.sequence()}"
        )
