"""One recorded drive operation of a commission (DRIVE_SPECIALIST_RFC §6): what `PassThroughCollector` holds."""
from __future__ import annotations

from dataclasses import dataclass

from .drive_outcome import DriveOutcome


@dataclass(frozen=True)
class CollectedResult:
    handle: str          # r1, r2, … in run order
    step: int            # the specialist's model turn that emitted the call
    kind: str            # the result kind its header names (search, listing, open, …)
    outcome: DriveOutcome
