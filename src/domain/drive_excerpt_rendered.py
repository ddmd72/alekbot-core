"""A rendered passages result (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A10): the text and the context plan it
printed, one typed contract for the reader's tool result and the run's ledger. Pure."""
from __future__ import annotations

from dataclasses import dataclass

from .drive_excerpt_context_plan import ContextPlan


@dataclass(frozen=True)
class RenderedExcerpts:
    text: str
    plan: ContextPlan
