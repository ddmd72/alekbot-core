from dataclasses import dataclass


@dataclass(frozen=True)
class ConsolidationRun:
    """The outcome of one consolidation request for a user."""

    processed: int = 0
    has_more: bool = False
    deferred: bool = False
    # a deferral or continuation had no queue to go to (local runs): nothing will pick the rest up on its own
    unattended: bool = False
