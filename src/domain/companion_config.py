"""
CompanionConfig — per-channel companion memory policy.

RFC: docs/10_rfcs/COMPANION_AGENTS_RFC.md §5-§6. Values, not code — the extractor
(which companion type) is a per-agent implementation choice; this is the per-channel
configuration of window/batch sizing, turn serialization, and the read-side toggle-set
that governs what a companion session may see of the user's personal store.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List

from .entities import FactDomain


class CompanionTextMode(str, Enum):
    """How a model turn is serialized into the extraction batch."""
    SUMMARY = "summary"
    FULL = "full"


@dataclass(frozen=True)
class CompanionConfig:
    """Per-channel companion memory policy (RFC §5-§6)."""
    window_threshold: int
    batch_size: int
    text_mode: CompanionTextMode = CompanionTextMode.SUMMARY

    # Read side — permission boundary, default is no (RFC §5).
    include_biographical: bool = False
    session_domains: List[FactDomain] = field(default_factory=list)
    include_standing_directives: bool = False
    include_own_records: bool = True
