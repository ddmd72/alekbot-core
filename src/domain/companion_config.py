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
    # SUMMARY is live for Tutor as of Phase G (2026-09-01): Tutor now emits a real
    # ≤300-char response_summary, so the extractor actually receives compressed text
    # instead of text==full_text. Dormant since Phase C (this default predates Phase G
    # by design, RFC §5) — was a no-op until Tutor had a summary to serialize. Keep
    # SUMMARY; do not flip to FULL, that would undermine the compression this phase
    # exists to deliver.
    text_mode: CompanionTextMode = CompanionTextMode.SUMMARY

    # Read side — permission boundary, default is no (RFC §5).
    include_biographical: bool = False
    session_domains: List[FactDomain] = field(default_factory=list)
    include_standing_directives: bool = False
    include_own_records: bool = True
