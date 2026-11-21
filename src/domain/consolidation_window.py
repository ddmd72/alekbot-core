"""The overflow window of a session: which of its oldest messages leave it as batches, and with which boundaries.

A batch is cut from the session only after its hand-off (owner decision O2), so a batch can be formed again by a later
append: its cut failed, or the append ran while its hand-off was still in flight. Its queue record is keyed by its exact
messages (`handoff_key`) and the queue keeps the first record, so the repeat must be the same run of messages. The
window values can change in between (a companion's window edited in the Cabinet, `ConsolidationSettings` on a deploy),
so each batch's boundaries are fixed when it is first formed: the session stores a plan, one `(size, handoff_key)` per
batch still at its head, and the next append forms those batches first, as planned, and only then applies the current
window to the rest. A planned batch no longer found at its place (the history was replaced or cut another way) ends the
plan there; a session without a plan forms every batch from the current values.
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .consolidation import handoff_key
from .llm import Message

PlannedBatch = Tuple[int, str]     # (size, handoff_key) of one batch at the head of the history, oldest first


@dataclass(frozen=True)
class OverflowWindow:
    """One append's window: `history` is what the session holds after the ceiling, `batches` its oldest batches past
    the window (planned ones first), `rest` the history after them, `plan` every batch of `batches` as stored for the
    next append, `dropped` the sizes the ceiling cut without a hand-off."""

    history: List[Message]
    batches: List[List[Message]]
    rest: List[Message]
    plan: List[PlannedBatch]
    dropped: List[int]


def overflow_window(session_id: str, history: Sequence[Message], plan: Sequence[PlannedBatch], max_length: int,
                    batch_size: int, ceiling: Optional[int]) -> OverflowWindow:
    """Form the batches of `history` past `max_length` messages: first the planned batches still at the head, then
    `batch_size` messages at a time while more than `max_length` remain. With a `ceiling`, a history longer than it
    first loses its oldest batches (a planned one by its own size)."""
    kept = list(history)
    owed = valid_plan(session_id, kept, plan)
    dropped: List[int] = []
    if ceiling is not None:
        while len(kept) > ceiling:
            size = owed.pop(0)[0] if owed else batch_size
            dropped.append(len(kept[:size]))
            kept = kept[size:]
    batches: List[List[Message]] = []
    position = 0
    for size, _ in owed:
        batches.append(kept[position:position + size])
        position += size
    new_plan = list(owed)
    while len(kept) - position > max_length:
        batch = kept[position:position + batch_size]
        batches.append(batch)
        new_plan.append((len(batch), handoff_key(session_id, batch)))
        position += len(batch)
    return OverflowWindow(history=kept, batches=batches, rest=kept[position:], plan=new_plan, dropped=dropped)


def valid_plan(session_id: str, history: Sequence[Message], plan: Sequence[PlannedBatch]) -> List[PlannedBatch]:
    """The leading planned batches that still stand at their place in `history`, each with its recorded messages."""
    valid: List[PlannedBatch] = []
    position = 0
    for size, key in plan:
        if size <= 0 or position + size > len(history):
            break
        if handoff_key(session_id, history[position:position + size]) != key:
            break
        valid.append((size, key))
        position += size
    return valid


def plan_after_cut(plan: Sequence[PlannedBatch], cut: int) -> List[PlannedBatch]:
    """The plan once the first `cut` messages left the history: the batches after them when the cut ends on a batch
    boundary, else none (a cut through a batch moves every later one off its place)."""
    position = 0
    for index, (size, _) in enumerate(plan):
        if position == cut:
            return list(plan[index:])
        position += size
    return []
