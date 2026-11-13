"""
Email search merge — the U rule: how the lists of a mail search become one ranking (email index v2 §5.3; search
slice 4). Pure: no I/O.

This module holds two public classes on purpose: a `Candidate` and the `RankedResult` that holds them are one value
shape, as `drive_tiers` holds `FileMatch` and `Cover`.

The **meaning list is the base**; the other lists add and re-rank, none removes (never a strict AND over what a model
filled in: a wrong slot would empty the answer). A card's rank group is the best list that found it: 0 About (or an
exact id or amount hit), 1 By translation, 2 Labelled, 3 found only by a slot list (people, labels, the window), 4 meaning
only (its similarity z above the floor). Inside a group, more lists first, then the meaning score, then the newest
display time. The one filter is the time: when the request's own text names a time, a card outside the window is out
whichever list found it.

A request word no card holds (D29, as the drive): the tier matches are dropped (strict AND cannot hold), the word is
named, `without` counts the cards that match the rest; the slot lists and the meaning list still rank. The answer is
`nothing` only when no list found a card and no word is absent.

Public interface:
    GROUP_ABOUT, GROUP_TRANSLATED, GROUP_LABELLED, GROUP_SLOT, GROUP_MEANING
    Candidate, RankedResult
    merge(...) -> RankedResult
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import AbstractSet, Dict, List, Mapping, Optional, Sequence, Tuple

from .drive_match import MatchTier
from .drive_tiers import FileMatch
from .email_card import EmailCard
from .email_card_time import when_of
from .email_search_time import INTENT_LATEST, TimeWindow, in_window

GROUP_ABOUT, GROUP_TRANSLATED, GROUP_LABELLED, GROUP_SLOT, GROUP_MEANING = 0, 1, 2, 3, 4
_TIER_GROUP: Mapping[MatchTier, int] = {
    MatchTier.ABOUT: GROUP_ABOUT, MatchTier.MENTIONED: GROUP_ABOUT,    # a card has no chunks: MENTIONED cannot occur
    MatchTier.TRANSLATED: GROUP_TRANSLATED, MatchTier.LABELLED: GROUP_LABELLED,
}


@dataclass(frozen=True)
class Candidate:
    """A card, its rank group, how many lists found it, its meaning score (None when that list did not rank it) and
    the keys that found it (they feed the result line's matched part and the fold stop)."""
    card: EmailCard
    group: int
    lists: int
    z: Optional[float]
    hit_keys: Tuple[str, ...]


@dataclass(frozen=True)
class RankedResult:
    """The ranking, best first, the request words no card holds, how many cards match the rest (None when nothing
    was left to match) and whether the answer is a final "nothing"."""
    ranked: Tuple[Candidate, ...]
    absent: Tuple[str, ...]
    without: Optional[int]
    nothing: bool


def merge(*, matches: Mapping[str, FileMatch], exact: Mapping[str, Tuple[str, ...]],
          people: Mapping[str, Tuple[str, ...]], labels: Mapping[str, Tuple[str, ...]],
          window_members: AbstractSet[str], meaning: Optional[Mapping[str, float]], floor: float,
          cards: Mapping[str, EmailCard], window: Optional[TimeWindow], today: date, absent: Sequence[str],
          without: Optional[int]) -> RankedResult:
    """The ranking of every list's cards (module docstring); an id with no card in `cards` is skipped."""
    tiers: Mapping[str, FileMatch] = {} if absent else matches
    meaning = meaning or {}
    ids = [*tiers, *exact, *people, *labels, *window_members, *(i for i, z in meaning.items() if z >= floor)]
    filtered = window is not None and window.filters and window.intent != INTENT_LATEST
    out: List[Candidate] = []
    for item_id in dict.fromkeys(ids):
        card = cards.get(item_id)
        if card is None or (filtered and window is not None and not in_window(card, window, today)):
            continue
        out.append(_candidate(card, tiers.get(item_id), exact.get(item_id), people.get(item_id),
                              labels.get(item_id), item_id in window_members, meaning.get(item_id), floor))
    latest = window is not None and window.intent == INTENT_LATEST
    out.sort(key=(lambda c: (c.group, -c.card.mail.sent_at.timestamp(), c.card.mail.email_id)) if latest
             else _rank_key)
    return RankedResult(ranked=tuple(out), absent=tuple(absent), without=without, nothing=not out and not absent)


# -- implementation -------------------------------------------------------------------------------------------------

def _candidate(card: EmailCard, match: Optional[FileMatch], exact: Optional[Tuple[str, ...]],
               people: Optional[Tuple[str, ...]], labels: Optional[Tuple[str, ...]], in_window_list: bool,
               z: Optional[float], floor: float) -> Candidate:
    groups: List[int] = []
    keys: Dict[str, None] = {}
    lists = 0
    if match is not None:
        groups.append(_TIER_GROUP.get(match.tier, GROUP_ABOUT))
        lists += 1
        keys.update(dict.fromkeys(k for cover in match.covers for k in cover.keys))
    for found in (exact, people, labels):
        if found is not None:
            lists += 1
            keys.update(dict.fromkeys(found))
    if exact is not None:
        groups.append(GROUP_ABOUT)
    if people is not None or labels is not None or in_window_list:
        groups.append(GROUP_SLOT)
    lists += 1 if in_window_list else 0
    if z is not None and z >= floor:
        groups.append(GROUP_MEANING)
        lists += 1
    return Candidate(card=card, group=min(groups), lists=lists, z=z, hit_keys=tuple(keys))


def _rank_key(c: Candidate) -> Tuple[int, int, float, int, str]:
    when = when_of(c.card.times, c.card.mail.sent_day)
    return (c.group, -c.lists, -(c.z if c.z is not None else -math.inf), -when.start.toordinal(),
            c.card.mail.email_id)
