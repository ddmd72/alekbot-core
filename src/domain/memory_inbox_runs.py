"""The two memory-inbox passes as the rhythm sees them (MCP_MEMORY_SUBMISSIONS_RFC §7, §9, §10): what a run asks of a
pass, what the pass answers, and the Protocols the triage and final passes implement.

The rhythm (the `/worker` runners) takes the account's memory-write lease, builds a `PassRequest` whose `deadline` is the
lease's end, and calls the pass; the pass reads and writes the inbox store itself and answers counts only (never text).
One module of small records and two Protocols, on purpose (a waiver of the one-public-class rule): one seam.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, Tuple


@dataclass(frozen=True)
class PassRequest:
    """One pass over one user's inbox. `zone` is the owner's IANA zone, `language` the owner's UI language (the
    candidates' language), `deadline` the end of the memory-write lease the caller holds (`lease_until`)."""

    user_id: str
    account_id: str
    zone: str
    language: str
    now: datetime
    deadline: datetime


@dataclass(frozen=True)
class TriageOutcome:
    """A triage run: `completed` (it ran), `empty` (nothing was `received`) or `failed` (a pass raised, timed out or
    used all its turns; decisions already written stand). Counts over the run: submissions sent, candidates made,
    submissions discarded, submissions still `received` after it (`left`), passes run. `sources` are the client names
    behind the new candidates (the notice names one when there is one)."""

    status: Literal["completed", "empty", "failed"]
    submissions: int
    candidates: int
    discarded: int
    left: int
    passes: int
    sources: Tuple[str, ...]


@dataclass(frozen=True)
class FinalOutcome:
    """A final run: `completed`, `empty` (nothing `confirmed`), `failed` (no pass ended) or `partial` (some passes
    ended, then one failed or the lease ran out). Counts: confirmed candidates sent, facts written, candidates given up."""

    status: Literal["completed", "empty", "failed", "partial"]
    candidates: int
    wrote: int
    gave_up: int


class TriagePass(Protocol):
    async def run(self, request: PassRequest) -> TriageOutcome: ...


class FinalPass(Protocol):
    async def run(self, request: PassRequest) -> FinalOutcome: ...
