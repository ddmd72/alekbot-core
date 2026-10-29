"""The pass-through collector of one drive commission (DRIVE_SPECIALIST_RFC §6).

Every tool handler of one execution records its operation's `DriveOutcome` here, with the model turn (step) it ran
in and the kind of result it is. After the run, code builds the answer from it (src/domain/drive_answer.py): every
read reaches the orchestrator verbatim, in run order, whatever the specialist's model said. No engine flag and no
change to the engine's fold: the collector also keeps the `drive_context` entries the engine would drop for a local
tool co-emitted with the terminal tool.

It also remembers the searches already run, so an identical `search_drive` is not run again (§5.4), and keeps the
run's passages ledger (USER_DRIVE_INDEXING_RFC §28.3 A11): every passage retrieved reaches the orchestrator once.

`CollectedResult`, the value type it holds, is in drive_collected_result.py.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .agent import DeliveryItem
from .drive_collected_result import CollectedResult
from .drive_excerpt_context_plan import ContextPlan
from .drive_excerpt_ledger import ExcerptLedger
from .drive_history import history_entries
from .drive_outcome import DriveOutcome

SearchKey = Tuple[str, ...]
_SEARCH_FIELDS = ("query", "folder", "kind", "date_from", "date_to")


def search_key(args: Mapping[str, Any]) -> SearchKey:
    """Two searches are identical when their normalised query, folder, kind and dates are (§5.4)."""
    return tuple(" ".join(str(args.get(name) or "").casefold().split()) for name in _SEARCH_FIELDS)


class PassThroughCollector:
    """Per execution, never shared. Handlers run on one event loop; `add` has no await, so it needs no lock."""

    def __init__(self) -> None:
        self._results: List[CollectedResult] = []
        self._searches: Dict[SearchKey, str] = {}
        self.repeats_skipped = 0
        self.ledger = ExcerptLedger()

    @property
    def results(self) -> Tuple[CollectedResult, ...]:
        return tuple(self._results)

    def add(self, step: int, kind: str, outcome: DriveOutcome) -> CollectedResult:
        result = CollectedResult(handle=f"r{len(self._results) + 1}", step=step, kind=kind, outcome=outcome)
        self._results.append(result)
        if outcome.excerpts is not None and outcome.excerpts.files:
            self.ledger.add(outcome.excerpts, outcome.excerpt_plan or ContextPlan.EMPTY)
        return result

    def earlier_search(self, key: SearchKey) -> Optional[str]:
        """The handle of an identical search already run, or None."""
        return self._searches.get(key)

    def remember_search(self, key: SearchKey, handle: str) -> None:
        self._searches.setdefault(key, handle)

    def drive_context(self) -> List[Dict[str, Any]]:
        """The concatenation, in order, of the per-operation entries, each naming what was done; no deduplication
        (§7.4): ConversationHandler dedups when it renders them (drive_history.py)."""
        entries: List[Dict[str, Any]] = []
        for result in self._results:
            entries.extend(history_entries(result.kind, result.outcome))
        return entries

    def note_context(self) -> List[str]:
        """Every note change's history line, in run order, no deduplication (specialist RFC §9.1, design Δ7)."""
        return [line for r in self._results for line in r.outcome.note_context]

    def delivery_items(self) -> List[DeliveryItem]:
        """Every note receipt and erase request, in run order, no deduplication (design Δ7: the collector, not the
        engine's fold, so a cut run keeps the receipt of a note it stored)."""
        return [item for r in self._results for item in r.outcome.delivery]

    def files(self) -> List[Dict[str, Any]]:
        """Every vision file, in run order (§6: several files reach the orchestrator as a list)."""
        return [r.outcome.file_data for r in self._results if r.outcome.file_data]
