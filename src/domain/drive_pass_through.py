"""The pass-through collector of one drive commission (DRIVE_SPECIALIST_RFC §6).

Every tool handler of one execution records its operation's `DriveOutcome` here, with the model turn (step) it ran
in and the kind of result it is. After the run, code builds the answer from it (src/domain/drive_answer.py): every
read reaches the orchestrator verbatim, in run order, whatever the specialist's model said. No engine flag and no
change to the engine's fold: the collector also keeps the `drive_context` entries the engine would drop for a local
tool co-emitted with the terminal tool.

It also remembers the searches already run, so an identical `search_drive` is not run again (§5.4).

Waiver (one public class per file): `CollectedResult` is the value type `PassThroughCollector` holds.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .drive_outcome import DriveOutcome

SearchKey = Tuple[str, ...]
_SEARCH_FIELDS = ("query", "folder", "kind", "date_from", "date_to")


def search_key(args: Mapping[str, Any]) -> SearchKey:
    """Two searches are identical when their normalised query, folder, kind and dates are (§5.4)."""
    return tuple(" ".join(str(args.get(name) or "").casefold().split()) for name in _SEARCH_FIELDS)


@dataclass(frozen=True)
class CollectedResult:
    handle: str          # r1, r2, … in run order
    step: int            # the specialist's model turn that emitted the call
    kind: str            # the result kind its header names (search, listing, open, …)
    outcome: DriveOutcome


class PassThroughCollector:
    """Per execution, never shared. Handlers run on one event loop; `add` has no await, so it needs no lock."""

    def __init__(self) -> None:
        self._results: List[CollectedResult] = []
        self._searches: Dict[SearchKey, str] = {}
        self.repeats_skipped = 0

    @property
    def results(self) -> Tuple[CollectedResult, ...]:
        return tuple(self._results)

    def add(self, step: int, kind: str, outcome: DriveOutcome) -> CollectedResult:
        result = CollectedResult(handle=f"r{len(self._results) + 1}", step=step, kind=kind, outcome=outcome)
        self._results.append(result)
        return result

    def earlier_search(self, key: SearchKey) -> Optional[str]:
        """The handle of an identical search already run, or None."""
        return self._searches.get(key)

    def remember_search(self, key: SearchKey, handle: str) -> None:
        self._searches.setdefault(key, handle)

    def drive_context(self) -> List[Dict[str, str]]:
        """The concatenation, in order, of today's per-operation entries; no deduplication (§7.4)."""
        entries: List[Dict[str, str]] = []
        for result in self._results:
            entries.extend(result.outcome.drive_context())
        return entries

    def files(self) -> List[Dict[str, Any]]:
        """Every vision file, in run order (§6: several files reach the orchestrator as a list)."""
        return [r.outcome.file_data for r in self._results if r.outcome.file_data]
