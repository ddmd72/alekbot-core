"""
The write gate after passages (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A14, OQ-A1, owner 2026-10-28). Pure.

On a run that is not a chat turn (a reminder, the daily review, any `notify` run, a call, a run with no origin), a
change is refused once passage text reached the model in an earlier step: passages are chosen by meaning, so an
on-topic planted instruction ranks in. A change co-emitted with the first passages read was decided before the text
was seen. Not `drive_note.is_owner_turn`, which counts a call as an owner turn: on a call the user is not reading what
the specialist read. A run with no origin is gated (fail-closed): only ConversationHandler marks a chat turn.

Public interface:
    content_gate_applies(context) -> bool
    write_refused(*, gated, step, passages_step) -> bool
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def content_gate_applies(context: Mapping[str, Any]) -> bool:
    """Off exactly on a chat turn: the test that makes `origin_label` say "chat"."""
    return not context.get("interactive_delivery")


def write_refused(*, gated: bool, step: int, passages_step: Optional[int]) -> bool:
    """`passages_step`: the first step whose passages text was returned to the model (None: none was)."""
    return gated and passages_step is not None and passages_step < step
