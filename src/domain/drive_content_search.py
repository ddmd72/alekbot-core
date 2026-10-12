"""
Content streams of drive search — the read fence, one hit per record, the term ranking and the window
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §7.4, §10.1 step 3, §10.2, §17.6, §24.6).

Pure: no I/O. The service reads chunks and records and calls these.

Public interface:
    ContentHit(record, chunk, score)                       a record's best chunk in one stream
    chunk_readable(chunk, record) -> bool                  the §7.4 fence
    pool_nearest(hits, records) -> List[ContentHit]        chunk kNN pooled per record, nearest first
    selective_term_hits(term_hits, limit) -> Dict[str, Sequence[DriveChunk]]   terms whose page was not full
    pool_terms(term_hits, records, terms) -> List[ContentHit]   exact-term hits pooled per record, most terms first
    window_indexes(index) -> Tuple[int, ...]               the window around a hit: index ± 1
    window_text(chunks) -> str                             the window's chunks joined without their overlap
    budget_windows(requests, terms, total) -> List[Window] every hit's best chunk, neighbours while the budget lasts
    matched_label(card_side, content_side) -> str          `matched` of a card (§10.2)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .drive_content_terms import content_terms
from .drive_index import DriveChunk, DriveIndexRecord, RecordState
from .drive_search import MATCHED_BOTH, MATCHED_CARD, MATCHED_CONTENT

_GAP = "\n[…]\n"   # between chunks that are not adjacent (a neighbour is missing)
_ELLIPSIS = "…"
_FOCUS_STEP = 40   # characters per step when locating a query term inside a chunk that must be trimmed


@dataclass(frozen=True)
class ContentHit:
    """A record's best chunk in one content stream: the window's anchor. `score` is the cosine distance (chunk
    stream, lower is better) or the number of query terms the record shares (term stream, higher is better)."""
    record: DriveIndexRecord
    chunk: DriveChunk
    score: float


def chunk_readable(chunk: DriveChunk, record: Optional[DriveIndexRecord]) -> bool:
    """§7.4: a chunk counts only if its record exists, is not being deleted and names the chunk's generation as the
    readable one. A re-index at the same cTag, a crash mid-write and a demotion are all invisible at once."""
    return (record is not None and record.state is not RecordState.DELETING
            and record.indexed_generation is not None and record.indexed_generation == chunk.generation)


def pool_nearest(hits: Sequence[Tuple[DriveChunk, float]],
                 records: Mapping[str, DriveIndexRecord]) -> List[ContentHit]:
    """Max-pool the chunk kNN to one hit per record, its nearest readable chunk (§10.1 step 3). Pooling comes before
    RRF (§17.6): `apply_rrf_ranking` keeps the first object per key, and a record with many hits must count once."""
    pooled: Dict[str, ContentHit] = {}
    for chunk, distance in sorted(hits, key=lambda h: (h[1], h[0].item_id, h[0].index)):
        record = records.get(chunk.item_id)
        if record is None or chunk.item_id in pooled or not chunk_readable(chunk, record):
            continue
        pooled[chunk.item_id] = ContentHit(record, chunk, distance)
    return list(pooled.values())


def selective_term_hits(term_hits: Mapping[str, Sequence[DriveChunk]],
                        limit: int) -> Dict[str, Sequence[DriveChunk]]:
    """The terms that select (§24.6, review D-M2): a term whose first page came back full (`limit` chunks or more)
    is held by more chunks than one page shows, and the page is ordered by item_id — ranking its files would rank by
    id, and anchoring their windows on it would show the common term, not the passage asked about. Such a term takes
    no part in the fusion or the windows, and is not read again on the widening. A page that was not full is
    complete."""
    return {term: chunks for term, chunks in term_hits.items() if len(chunks) < limit}


def pool_terms(term_hits: Mapping[str, Sequence[DriveChunk]], records: Mapping[str, DriveIndexRecord],
               terms: Sequence[str]) -> List[ContentHit]:
    """Pool the exact-term hits per record (§24.6): records sharing more query terms first, then the more specific
    term (`terms` is most specific first), then item_id. The anchor is the record's chunk holding the most terms,
    the lowest index on a tie."""
    position = {term: i for i, term in enumerate(terms)}
    per_chunk: Dict[Tuple[str, int, int], Set[str]] = {}
    chunks: Dict[Tuple[str, int, int], DriveChunk] = {}
    for term, found in term_hits.items():
        for chunk in found:
            if not chunk_readable(chunk, records.get(chunk.item_id)):
                continue
            key = (chunk.item_id, chunk.generation, chunk.index)
            per_chunk.setdefault(key, set()).add(term)
            chunks[key] = chunk
    shared: Dict[str, Set[str]] = {}
    anchor: Dict[str, Tuple[str, int, int]] = {}
    for key in sorted(per_chunk):
        item_id = key[0]
        shared.setdefault(item_id, set()).update(per_chunk[key])
        best = anchor.get(item_id)
        if best is None or len(per_chunk[key]) > len(per_chunk[best]):
            anchor[item_id] = key

    def rank(item_id: str) -> Tuple[int, int, str]:
        return -len(shared[item_id]), min(position.get(t, len(position)) for t in shared[item_id]), item_id

    return [ContentHit(records[item_id], chunks[anchor[item_id]], float(len(shared[item_id])))
            for item_id in sorted(shared, key=rank)]


def window_indexes(index: int) -> Tuple[int, ...]:
    """The best chunk and its neighbours of the same generation (§3.3, §10.2)."""
    return tuple(i for i in (index - 1, index, index + 1) if i >= 0)


def window_text(chunks: Sequence[DriveChunk]) -> str:
    """Join a window's chunks in index order. Adjacent chunks overlap (CHUNK_OVERLAP, §8.4): the repeated text is
    cut by the character offsets; a missing neighbour leaves a visible gap marker. Adjacent chunks that do not
    overlap are separated only by the whitespace the chunker leaves out of every span, and a continued table's
    chunk carries its repeated header in front of its span (drive_chunker): both join without a gap marker."""
    ordered = sorted(chunks, key=lambda c: c.index)
    if not ordered:
        return ""
    parts = [ordered[0].text]
    for prev, cur in zip(ordered, ordered[1:]):
        overlap = prev.char_end - cur.char_start
        prefix = len(cur.text) - (cur.char_end - cur.char_start)   # > 0: a repeated table header
        body = cur.text[prefix:] if prefix > 0 and overlap <= 0 else cur.text
        if cur.index != prev.index + 1:
            parts.append(_GAP + cur.text)
        elif 0 < overlap <= len(cur.text):
            parts.append(cur.text[overlap:])
        elif overlap == 0:
            parts.append(body)
        elif overlap < 0:
            parts.append(("\n" if overlap == -1 else "\n\n") + body)   # only whitespace lies between them
        else:
            parts.append(_GAP + cur.text)   # offsets that do not fit the text: never cut blindly
    return "".join(parts)


@dataclass(frozen=True)
class WindowRequest:
    """One content hit's window, in result rank order: the anchor's index and the chunks read around it."""
    anchor_index: int
    chunks: Sequence[DriveChunk]


@dataclass(frozen=True)
class Window:
    text: str
    shortened: bool = False                   # the best chunk was trimmed, or a neighbour left out for the budget


def _focus_end(text: str, terms: Sequence[str]) -> Optional[int]:
    """Where the first of `terms` ends in `text` (the shortest prefix whose content terms hold one), or None."""
    if not terms:
        return None
    wanted = set(terms)
    for end in range(_FOCUS_STEP, len(text) + _FOCUS_STEP, _FOCUS_STEP):
        if wanted & set(content_terms(text[:end])):
            return min(end, len(text))
    return None


def _trim(text: str, limit: int, terms: Sequence[str]) -> str:
    """At most `limit` characters of `text`, around the first query term it holds, else its beginning; a cut side
    is marked with an ellipsis."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return _ELLIPSIS[:limit]
    focus = _focus_end(text, terms)
    start = 0 if focus is None else max(0, min(focus - limit // 2, len(text) - limit))
    piece = text[start:start + limit]
    if start > 0:
        piece = _ELLIPSIS + piece[1:]
    if start + limit < len(text):
        piece = piece[:-1] + _ELLIPSIS
    return piece


def _fair_share(sizes: Sequence[int], total: int) -> int:
    """The largest per-chunk length `c` with Σ min(size, c) <= total: no chunk is cut while all fit, and a cut takes
    only from the chunks longer than what the shorter ones leave (water-filling, review D minor)."""
    if not sizes or sum(sizes) <= total:
        return max(sizes, default=0)
    left, count = total, len(sizes)
    for size in sorted(sizes):
        if size * count > left:
            return max(1, left // count)
        left -= size
        count -= 1
    return max(1, left)


def budget_windows(requests: Sequence[WindowRequest], terms: Sequence[str], total: int) -> List[Window]:
    """Fit every content hit's window into `total` characters (§10.2). Each hit with a stored best chunk keeps it,
    trimmed around its query term only when the best chunks alone would not fit, and then only down to the share
    the shorter ones leave (`_fair_share`); then neighbours are
    added in rank order, each whole, until one does not fit — after that no lower hit gets one, so a better hit
    never has less context than a worse one. A hit whose best chunk is gone (collected meanwhile) gets no window."""
    anchors: Dict[int, DriveChunk] = {}
    for i, request in enumerate(requests):
        anchor = next((c for c in request.chunks if c.index == request.anchor_index and c.text), None)
        if anchor is not None:
            anchors[i] = anchor
    share = _fair_share([len(a.text) for a in anchors.values()], total)
    best = {i: _trim(a.text, share, [t for t in terms if t in a.content_terms]) for i, a in anchors.items()}
    trimmed = {i for i, a in anchors.items() if best[i] != a.text}
    remaining = total - sum(len(t) for t in best.values())
    included: Dict[int, List[DriveChunk]] = {i: [a] for i, a in anchors.items()}
    left_out: Set[int] = set()
    stopped = False
    for i in sorted(anchors):
        if i in trimmed:
            continue   # a trimmed chunk is not joined to its neighbours
        anchor = anchors[i]
        neighbours = sorted((c for c in requests[i].chunks
                             if c.index in (anchor.index - 1, anchor.index + 1) and c.text), key=lambda c: c.index)
        for neighbour in neighbours:
            cost = len(window_text(included[i] + [neighbour])) - len(window_text(included[i]))
            if stopped or cost > remaining:
                stopped = True
                left_out.add(i)
                continue
            included[i].append(neighbour)
            remaining -= cost
    windows: List[Window] = []
    for i in range(len(requests)):
        if i not in anchors:
            windows.append(Window(""))
        elif i in trimmed:
            windows.append(Window(best[i], shortened=True))
        else:
            windows.append(Window(window_text(included[i]), shortened=i in left_out))
    return windows


def matched_label(card_side: bool, content_side: bool) -> str:
    """Which side found the record (§10.2): its card (card or name stream), its content, or both."""
    if card_side and content_side:
        return MATCHED_BOTH
    return MATCHED_CONTENT if content_side else MATCHED_CARD
