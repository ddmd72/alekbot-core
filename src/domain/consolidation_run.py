from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConsolidationRun:
    """The outcome of one consolidation request for a user."""

    processed: int = 0
    has_more: bool = False
    deferred: bool = False
    # a deferral or continuation had no queue to go to (local runs); a flag about the wiring, not part of the outcome
    unattended: bool = field(default=False, compare=False)
