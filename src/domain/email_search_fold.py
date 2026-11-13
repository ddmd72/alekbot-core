"""
Email search fold — template mail folded into one line (email index v2 §5.4; search slice 4). Pure: no I/O.

Receipts, balance alerts and grade notices of one sender share a template key (`email_card_keys.template_key`: the
sender's domain and the subject with every number, date and month masked); listing each would bury the answer. The ranked
candidates are walked; those sharing a template key form one group, placed where its best-ranked member stands, and
its line shows the group's **newest** member with that member's hit keys; the others are the line's `folded` members.

Only an exact id or amount hit stops a card from folding (`stops_folding`): a sender or word match never does, or
templates would never fold. Folding goes inside each rank group alone: a card found only by meaning never folds into
a matched one. Fields are code's alone (the template key); no model decides what is the same kind.

Public interface:
    FoldedLine                   the shown card, the keys that matched it, the members it stands for, its rank group
    fold(candidates) -> (FoldedLine, ...)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .email_card import EmailCard
from .email_card_keys import stops_folding
from .email_search_merge import GROUP_ABOUT, Candidate


@dataclass(frozen=True)
class FoldedLine:
    """One line of a result: `card` with the keys that `matched` it, and the `folded` members it stands for (their
    own lines are not shown). `group` is the rank group the line sits in."""
    card: EmailCard
    matched: Tuple[str, ...]
    folded: Tuple[EmailCard, ...]
    group: int = GROUP_ABOUT


def fold(candidates: Sequence[Candidate]) -> Tuple[FoldedLine, ...]:
    """The lines of the ranked candidates, in rank order (module docstring)."""
    slots: List[Tuple[Tuple[int, str], List[Candidate]]] = []
    index: Dict[Tuple[int, str], int] = {}
    for c in candidates:
        key = (c.group, c.card.template_key)
        if stops_folding(c.card, c.hit_keys):
            slots.append((key, [c]))
        elif key in index:
            slots[index[key]][1].append(c)
        else:
            index[key] = len(slots)
            slots.append((key, [c]))
    return tuple(_line(members) for _, members in slots)


def _line(members: Sequence[Candidate]) -> FoldedLine:
    newest = max(members, key=lambda c: (c.card.mail.sent_at, c.card.mail.email_id))
    others = sorted((c.card for c in members if c is not newest),
                    key=lambda card: (card.mail.sent_at, card.mail.email_id), reverse=True)
    return FoldedLine(card=newest.card, matched=newest.hit_keys, folded=tuple(others), group=newest.group)
