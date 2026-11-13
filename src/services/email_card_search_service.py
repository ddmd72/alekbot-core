"""
EmailCardSearchService — email search v2 over the card index (email index v2 §5; search slice 4). Streams in, domain
out: every ranking decision is a pure function (`email_search_*`), this service only reads the streams and hands them
over. Not wired into the application yet (composition, the Firestore read adapter and the query-slots agent are slice 8).

Two modes. **List**: the request's text names a period and nothing else (no topic word, no exact key, no person, no
type or area slot): every carded mail of the period, newest sent first, folded, with a count line; no embedding call (a
kNN page saw a third of "mail for September", the key stream sees all of it). **Ranked**: the word streams (`about_keys`
and `people_keys` by the concept's about keys, `name_keys` by its name keys) go through the drive's tier classifier
(strict AND with the drive's allowances); the exact-key, people, label and window streams add; the meaning list (the
kNN page of the card vectors) is the base; `merge` ranks them all (the U rule).

One INFO line per search, never the request's text or any mail content.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..domain.confirmed_people import ConfirmedPeople
from ..domain.drive_keys import fetch_batches
from ..domain.drive_match import Concept
from ..domain.drive_tier_pool import ConceptHits, document_frequency, document_frequency_common
from ..domain.drive_tiers import (
    FileKeys, FileMatch, allowances, classify, name_query_keys, related_floor, size_band, source_scores,
)
from ..domain.email_card import EmailCard
from ..domain.email_search_fold import fold
from ..domain.email_search_merge import GROUP_ABOUT, GROUP_MEANING, Candidate, merge
from ..domain.email_search_params import (
    KEY_PAGE, LIST_LINES_MAX, LIST_PAGE, MEANING_TOP, MONTHS_PER_FETCH,
)
from ..domain.email_search_query import (
    MailConcepts, MailQuerySlots, MailSearchRequest, exact_query_keys, label_query_keys, mail_concepts,
    people_query_keys,
)
from ..domain.email_search_render import MailSearchResult, render_list, render_ranked
from ..domain.email_search_time import INTENT_LATEST, TimeWindow, choose_window, in_window, window_months
from ..ports.confirmed_people_repository import ConfirmedPeopleRepository
from ..ports.email_card_index import EmailCardIndex
from ..ports.embedding_service import EmbeddingService
from ..utils.logger import exc_brief, logger

_USER_PREFIX_CHARS = 8


@dataclass
class _Streams:
    """What the streams read in one search: the cards by id and each list's hits."""
    cards: Dict[str, EmailCard]
    matches: Dict[str, FileMatch]
    exact: Dict[str, Tuple[str, ...]]
    people: Dict[str, Tuple[str, ...]]
    labels: Dict[str, Tuple[str, ...]]
    window: Set[str]
    meaning: Optional[Dict[str, float]]
    absent: Tuple[str, ...]
    without: Optional[int]


class EmailCardSearchService:
    def __init__(self, index: EmailCardIndex, embeddings: EmbeddingService, people: ConfirmedPeopleRepository) -> None:
        self._index = index
        self._embeddings = embeddings
        self._people = people

    async def search(self, user_id: str, account_id: str, request: MailSearchRequest) -> MailSearchResult:
        """The result block for one mail request (module docstring)."""
        started = time.monotonic()
        concepts = mail_concepts(request.text)
        slots = request.slots
        window = choose_window(concepts, slots, request.today)
        confirmed = await self._confirmed(account_id) if slots is not None and slots.people else None
        people_keys = people_query_keys(slots.people, confirmed) if slots is not None else ()
        label_keys, exact_keys = label_query_keys(slots), exact_query_keys(slots)
        uncarded = await self._index.count_uncarded(user_id)
        list_window = _list_window(concepts, slots, window, people_keys, label_keys, exact_keys)
        if list_window is not None:
            result, candidates = await self._list(user_id, list_window, request.today, uncarded)
            self._log(user_id, "list", concepts, (), candidates, result, 0, uncarded, started)
            return result
        carded = max(await self._index.count_carded(user_id), 1)
        streams = await self._streams(user_id, request, concepts, window, people_keys, label_keys, exact_keys, carded)
        ranked = merge(matches=streams.matches, exact=streams.exact, people=streams.people, labels=streams.labels,
                       window_members=streams.window, meaning=streams.meaning,
                       floor=related_floor(size_band(carded)), cards=streams.cards, window=window,
                       today=request.today, absent=streams.absent, without=streams.without)
        lines = fold(ranked.ranked)
        matched_lines = tuple(line for line in lines if line.group < GROUP_MEANING)
        meaning_lines = tuple(line for line in lines if line.group == GROUP_MEANING)
        total = sum(1 for c in ranked.ranked if c.group < GROUP_MEANING)
        result = render_ranked(ranked, matched_lines, meaning_lines, total=total,
                               scope_all=slots is not None and slots.scope == "all", uncarded=uncarded,
                               has_topic=bool(concepts.topic))
        self._log(user_id, result.mode, concepts, ranked.absent, len(ranked.ranked), result, carded, uncarded,
                  started)
        return result

    # -- list mode --------------------------------------------------------------------------------------------------

    async def _list(self, user_id: str, window: TimeWindow, today: date,
                    uncarded: int) -> Tuple[MailSearchResult, int]:
        months = window_months(window, today)
        pages = await asyncio.gather(*(self._index.list_in_months(user_id, batch, LIST_PAGE)
                                       for batch in _chunks(months, MONTHS_PER_FETCH)))
        found = {c.mail.email_id: c for page in pages for c in page if in_window(c, window, today)}
        newest_first = sorted(found.values(), key=lambda c: (c.mail.sent_at, c.mail.email_id), reverse=True)
        lines = fold([Candidate(card=c, group=GROUP_ABOUT, lists=1, z=None, hit_keys=()) for c in newest_first])
        return render_list(window, lines[:LIST_LINES_MAX], count=len(newest_first), uncarded=uncarded), len(found)

    # -- ranked mode ------------------------------------------------------------------------------------------------

    async def _streams(self, user_id: str, request: MailSearchRequest, concepts: MailConcepts,
                       window: Optional[TimeWindow], people_keys: Sequence[str], label_keys: Sequence[str],
                       exact_keys: Sequence[str], carded: int) -> _Streams:
        cards: Dict[str, EmailCard] = {}
        slots = request.slots
        hits_task = asyncio.gather(*(self._concept_hits(user_id, c, cards) for c in concepts.topic))
        exact_task = self._stream(user_id, ("about_keys",), exact_keys, cards)
        people_task = self._stream(user_id, ("people_keys", "about_keys"), people_keys, cards)
        label_task = self._stream(user_id, ("about_keys",), label_keys, cards)
        window_task = self._window_members(user_id, window, request.today, cards)
        meaning_task = self._meaning(user_id, slots, request.text, carded, cards)
        hits, exact, people, labels, members, meaning = await asyncio.gather(
            hits_task, exact_task, people_task, label_task, window_task, meaning_task)
        matches, absent, without = _classify(concepts.topic, list(hits), cards, carded)
        return _Streams(cards=cards, matches=matches, exact=exact, people=people, labels=labels, window=members,
                        meaning=meaning, absent=absent, without=without)

    async def _concept_hits(self, user_id: str, concept: Concept, cards: Dict[str, EmailCard]) -> ConceptHits:
        """One concept's hits: its about keys against `about_keys` and `people_keys`, its name keys against
        `name_keys`; `full` when any page was full (the document frequency is then a lower bound)."""
        jobs = [(field, batch) for batch in fetch_batches(concept.about_keys) for field in ("about_keys", "people_keys")]
        jobs += [("name_keys", batch) for batch in fetch_batches(tuple(sorted(name_query_keys(concept))))]
        pages = await asyncio.gather(*(self._index.find_by_keys(user_id, field, batch, KEY_PAGE)
                                       for field, batch in jobs))
        hits = ConceptHits()
        for (field, batch), page in zip(jobs, pages):
            hits.full = hits.full or len(page) >= KEY_PAGE
            for card in page:
                item_id = card.mail.email_id
                cards[item_id] = card
                if field == "name_keys":
                    hits.names.add(item_id)
                else:
                    hits.add_about(item_id, set(batch) & set(getattr(card, field)))
        return hits

    async def _stream(self, user_id: str, fields: Sequence[str], keys: Sequence[str],
                      cards: Dict[str, EmailCard]) -> Dict[str, Tuple[str, ...]]:
        """The cards holding any of `keys` in any of `fields`, each with the keys it holds."""
        jobs = [(field, batch) for batch in fetch_batches(tuple(keys)) for field in fields]
        pages = await asyncio.gather(*(self._index.find_by_keys(user_id, field, batch, KEY_PAGE)
                                       for field, batch in jobs))
        found: Dict[str, Dict[str, None]] = {}
        for (field, batch), page in zip(jobs, pages):
            for card in page:
                cards[card.mail.email_id] = card
                held = found.setdefault(card.mail.email_id, {})
                held.update(dict.fromkeys(k for k in batch if k in getattr(card, field)))
        return {item_id: tuple(held) for item_id, held in found.items()}

    async def _window_members(self, user_id: str, window: Optional[TimeWindow], today: date,
                              cards: Dict[str, EmailCard]) -> Set[str]:
        if window is None or window.intent == INTENT_LATEST:
            return set()
        pages = await asyncio.gather(*(self._index.list_in_months(user_id, batch, LIST_PAGE)
                                       for batch in _chunks(window_months(window, today), MONTHS_PER_FETCH)))
        members: Set[str] = set()
        for page in pages:
            for card in page:
                if in_window(card, window, today):
                    cards[card.mail.email_id] = card
                    members.add(card.mail.email_id)
        return members

    async def _meaning(self, user_id: str, slots: Optional[MailQuerySlots], text: str, carded: int,
                       cards: Dict[str, EmailCard]) -> Optional[Dict[str, float]]:
        """The kNN page's robust z per card; None when the source abstains (a small pool) or fails (logged)."""
        try:
            vector = await self._embeddings.get_embedding((slots.subject if slots else "") or text, "RETRIEVAL_QUERY")
            nearest = await self._index.find_nearest(user_id, vector, MEANING_TOP)
        except Exception as exc:  # the meaning list only adds: a failed read must not fail the search
            logger.warning(f"[EmailSearchV2] meaning list skipped: {exc_brief(exc)}")
            return None
        for card, _ in nearest:
            cards[card.mail.email_id] = card
        return source_scores({card.mail.email_id: 1.0 - distance for card, distance in nearest})

    async def _confirmed(self, account_id: str) -> Optional[ConfirmedPeople]:
        try:
            return await self._people.get(account_id)
        except ValueError as exc:   # an unreadable list is not an empty one, but search goes on without it
            logger.warning(f"[EmailSearchV2] confirmed people list unreadable, searched without it: {exc_brief(exc)}")
            return None

    def _log(self, user_id: str, mode: str, concepts: MailConcepts, absent: Sequence[str], candidates: int,
             result: MailSearchResult, carded: int, uncarded: int, started: float) -> None:
        logger.info(
            f"[EmailSearchV2] user={user_id[:_USER_PREFIX_CHARS]} mode={mode} topic={len(concepts.topic)} "
            f"time={len(concepts.time)} absent={len(absent)} candidates={candidates} shown={len(result.shown_ids)} "
            f"carded={carded} uncarded={uncarded} ms={int((time.monotonic() - started) * 1000)}")


def _list_window(concepts: MailConcepts, slots: Optional[MailQuerySlots], window: Optional[TimeWindow],
                 people_keys: Sequence[str], label_keys: Sequence[str], exact_keys: Sequence[str]) -> Optional[TimeWindow]:
    """The window to list when the text names a period and nothing else (module docstring), else None."""
    if window is None or not window.filters or window.intent == INTENT_LATEST or concepts.topic:
        return None
    named = slots is not None and (slots.people or slots.doc_types or slots.domains or slots.exact_keys)
    return None if (named or people_keys or label_keys or exact_keys) else window


def _classify(topic: Sequence[Concept], hits: Sequence[ConceptHits], cards: Mapping[str, EmailCard],
              carded: int) -> Tuple[Dict[str, FileMatch], Tuple[str, ...], Optional[int]]:
    """The strict-AND tier matches of the topic words, the words no card holds and how many cards match the rest
    (the drive's D29: with an absent word the matches are not shown, only counted)."""
    if not topic:
        return {}, (), None
    df, _ = document_frequency(hits)
    absent = tuple(c.word for c, n in zip(topic, df) if n == 0)
    tested = [c for c, n in zip(topic, df) if n > 0]
    df_of = {c.word: n for c, n in zip(topic, document_frequency_common(hits))}
    allowance = allowances(tested, df_of, carded)
    matches: Dict[str, FileMatch] = {}
    if tested:
        for item_id in sorted(set().union(*(h.files() for h in hits))):
            card = cards[item_id]
            keys = FileKeys(item_id=item_id, about_keys=frozenset(card.about_keys) | frozenset(card.people_keys),
                            name_keys=frozenset(card.name_keys))
            match = classify(keys, tested, allowance, {}, {})
            if match is not None:
                matches[item_id] = match
    without = len(matches) if absent and tested else None
    return matches, absent, without


def _chunks(items: Sequence[str], size: int) -> List[Tuple[str, ...]]:
    return [tuple(items[i:i + size]) for i in range(0, len(items), size)]
