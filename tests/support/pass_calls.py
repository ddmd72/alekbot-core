"""Typed pass calls for the pass-stats tests (Facts S4 T6b, S1–S3 review L2).

The stats read `PassCall`s, which the agent builds from each tool call (`consolidation_agent._pass_call`). These
builders give the values the agent builds for the old scenarios' calls: a create or merge keyed by its text, with its
sources and domain; an update or ask keyed by its fact id, `closing` when its state closes the fact. The fingerprint
names the call's inputs, so two calls with the same inputs are a repeat."""
from typing import Iterable, Optional, Sequence

from src.domain.consolidation_pass_stats import ConsolidationPassStats, PassCall, PassTool
from src.domain.entities import FactDomain
from src.domain.pass_actor import PassActor

__all__ = ["ask", "count", "create", "merge", "search", "stats", "turn", "update"]


def stats(label: str = "stage1", max_turns: int = 15) -> ConsolidationPassStats:
    return ConsolidationPassStats(PassActor(label), max_turns)


def turn(s: ConsolidationPassStats, calls: Sequence[PassCall], outcomes: Sequence[str]) -> None:
    s.record_turn(calls)
    s.record_outcomes(calls, outcomes)


def _domain(domain: Optional[str]) -> Optional[FactDomain]:
    return FactDomain(domain) if domain else None


def create(text: str, *, derived: Optional[Iterable[str]] = None, domain: Optional[str] = None) -> PassCall:
    sources = frozenset(derived or ())
    return PassCall.with_text(
        PassTool.CREATE, f"create:{text}:{sorted(sources)}:{domain}", text, sources=sources, domain=_domain(domain),
    )


def merge(text: str, ids: Iterable[str] = ("a", "b"), domain: Optional[str] = None) -> PassCall:
    sources = frozenset(ids)
    return PassCall.with_text(
        PassTool.MERGE, f"merge:{text}:{sorted(sources)}:{domain}", text, sources=sources, domain=_domain(domain),
    )


def update(fact_id: str, *, closing: bool = False, **updates: object) -> PassCall:
    """An update of `fact_id`; `updates` only tell calls apart (the stats read no argument)."""
    return PassCall(PassTool.UPDATE, f"update:{fact_id}:{closing}:{sorted(updates.items())}", key=fact_id,
                    closing=closing)


def ask(fact_id: str) -> PassCall:
    return PassCall(PassTool.ASK, f"ask:{fact_id}", key=fact_id)


def search(query: str = "") -> PassCall:
    return PassCall(PassTool.SEARCH, f"search:{query}")


def count(text: str = "") -> PassCall:
    return PassCall(PassTool.COUNT, f"count:{text}")
