"""
Drive tiers — how one file matched a Search v2 query, and the meaning score of the unverified tier
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.5.3, §25.5.4). Pure: stdlib only, no I/O.

A concept is covered in a file by a key of one of four source classes (§25.2.4): verified record (about keys `e:`
`v:` `y:` `m:` `k:`; name keys `w:` `n:` `y:`), passage words (one chunk's `w:` `n:` `m:`), translation (a skeleton
`x:`/`ex:`, or the same chunk's hint keys) and automatic record (`lw:` `tw:` `dw:` `lv:` `pw:` `pe:` `pn:`). A file's
tier is set by the weakest class it NEEDS: every concept takes its strongest cover, chunk covers come from ONE chunk,
and two or more chunk-covered concepts must sit in a minimum window of at most PROXIMITY_CHARS (record-covered ones
are exempt). Strict AND (D28): an absent concept (`df = 0`) blocks every tier but Related; with three or more concepts
a file may miss the single lowest-weight concept; a common concept (held by more than COMMON_SHARE of the carded
files) may be missed too. Fewer misses beat a better tier: a file matching every word automatically is not shown as
missing one.

Public interface:
    FileKeys, Cover, FileMatch, Allowance
    allowances(concepts, df, carded) -> Allowance;  concept_weight(df, carded) -> float
    record_cover(file, concept) / chunk_cover(file, concept, chunk_id) -> Optional[Cover]
    chunks_needing_text(file, concepts) -> (ChunkId, ...)     the chunks whose text decides proximity
    cover_spans(cover, text, items) -> ((start, end), ...)    where a chunk or hint cover sits in the chunk text
    classify(file, concepts, allowance, texts, items) -> Optional[FileMatch]
    min_window(span_lists) -> Optional[int]
    source_scores(sims) -> Optional[{item_id: z}];  long_doc_correction(n);  meaning_scores(...)
    size_band(carded) -> int;  related_floor(band) -> float
    CLASS_RANK, TIER_OF_RANK
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from statistics import median
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from .drive_enrichment import HintItem
from .drive_index_lean import ChunkId
from .drive_index_params import (
    COMMON_SHARE, DRIVE_SIZE_BANDS, MEANING_POOL, MEANING_POOL_MIN, MIN_SPREAD, PROXIMITY_CHARS, Z_RELATED,
)
from .drive_keys import (
    match_spans, source_class_of_about_key, source_class_of_name_key, source_class_of_word_key, word_form_keys,
)
from .drive_match import Concept, KeyFamily, MatchTier, SourceClass

Span = Tuple[int, int]

CLASS_RANK: Mapping[SourceClass, int] = {
    SourceClass.VERIFIED_RECORD: 0, SourceClass.PASSAGE_WORDS: 1, SourceClass.TRANSLATION: 2,
    SourceClass.AUTOMATIC_RECORD: 3,
}
TIER_OF_RANK: Mapping[int, MatchTier] = {
    0: MatchTier.ABOUT, 1: MatchTier.MENTIONED, 2: MatchTier.TRANSLATED, 3: MatchTier.LABELLED,
}
WHERE_RECORD = "record"     # the record's about keys
WHERE_NAME = "name"         # the record's name keys
WHERE_CHUNK = "chunk"       # one chunk's word keys
WHERE_HINT = "hint"         # the same chunk's hint document
MISSED_COMMON = "common"
MISSED_LOW_WEIGHT = "low_weight"
_SUBSET_SEARCH_MAX = 10     # optional chunk concepts above which only "all" and "none" are tried


@dataclass(frozen=True)
class FileKeys:
    """What coverage is tested on (§25.5.2): the record's about and name keys, and the fenced keys of the chunks and
    hint documents the streams named for this file."""
    item_id: str
    about_keys: FrozenSet[str] = frozenset()
    name_keys: FrozenSet[str] = frozenset()
    chunk_keys: Mapping[ChunkId, FrozenSet[str]] = field(default_factory=dict)
    hint_keys: Mapping[ChunkId, FrozenSet[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Cover:
    """How one concept is covered in one file: its class, where (record, name, chunk, hint) and the keys that
    matched."""
    word: str
    source_class: SourceClass
    where: str
    keys: Tuple[str, ...]


@dataclass(frozen=True)
class FileMatch:
    """A file's best tier: the covers it needs, the words it may miss (word, MISSED_*) and the chunk its chunk covers
    come from."""
    item_id: str
    tier: MatchTier
    covers: Tuple[Cover, ...]
    missed: Tuple[Tuple[str, str], ...] = ()
    chunk: Optional[ChunkId] = None


@dataclass(frozen=True)
class Allowance:
    """Strict AND's exceptions for one query (§25.5.4): words held by no file (block), common words (may be missed,
    named), and the one lowest-weight word a file may miss with three or more concepts."""
    absent: Tuple[str, ...] = ()
    common: FrozenSet[str] = frozenset()
    low_weight: Optional[str] = None


# -- strict AND ------------------------------------------------------------------------------------------------------

def concept_weight(df: int, carded: int) -> float:
    """`log(1 + N / df)`, N the carded files; an absent concept has no weight (it blocks)."""
    return math.log(1 + max(carded, 1) / df) if df > 0 else math.inf


def allowances(concepts: Sequence[Concept], df: Mapping[str, int], carded: int) -> Allowance:
    """The query's allowances from each concept's user-wide document frequency. A common word is missable only when
    some concept is not common (a lone common word is still the subject); a number is never common, and never the
    low-weight word."""
    words = [c.word for c in concepts]
    absent = tuple(w for w in words if df.get(w, 0) == 0)
    # a number (an amount, an id, a year) is precise: never missable as common (P6 review m7)
    common = frozenset(c.word for c in concepts
                       if not c.is_number and df.get(c.word, 0) > COMMON_SHARE * max(carded, 1))
    if not set(words) - common:
        common = frozenset()
    low: Optional[str] = None
    # a number (an amount, an id, a year) is never the word a file may miss either: "electricity bill 2024" must not
    # return a 2026 bill (P7 eval; the m7 ruling) — the lowest-weight WORD is
    present = [(df[c.word], i, c.word) for i, c in enumerate(concepts)
               if not c.is_number and df.get(c.word, 0) > 0]
    if len(words) >= 3 and present:
        low = max(present)[2]   # the lowest weight is the highest df; ties: the later word
    return Allowance(absent=absent, common=common, low_weight=low)


# -- covers ----------------------------------------------------------------------------------------------------------

def _rank(cover: Optional[Cover]) -> int:
    return CLASS_RANK[cover.source_class] if cover is not None else 99


def _best(word: str, matched: Sequence[str], classify_key: object, where: str) -> Optional[Cover]:
    by_class: Dict[SourceClass, List[str]] = {}
    for key in matched:
        cls = classify_key(key)  # type: ignore[operator]
        if cls is not None:
            by_class.setdefault(cls, []).append(key)
    if not by_class:
        return None
    cls = min(by_class, key=lambda c: CLASS_RANK[c])
    return Cover(word, cls, where, tuple(sorted(by_class[cls])))


def name_query_keys(concept: Concept) -> FrozenSet[str]:
    """The name keys a concept is tested on: its chunk keys (`w:`, `x:`, `n:`, `m:`) and its `y:` keys."""
    years = (k for k in concept.about_keys if k.startswith(KeyFamily.YEAR.value))
    return frozenset(concept.word_keys) | frozenset(years)


def record_cover(file: FileKeys, concept: Concept) -> Optional[Cover]:
    """The concept's strongest record-level cover: about keys first, then name keys (a tie keeps the about key)."""
    about = _best(concept.word, sorted(file.about_keys & set(concept.about_keys)), source_class_of_about_key,
                  WHERE_RECORD)
    name = _best(concept.word, sorted(file.name_keys & name_query_keys(concept)), source_class_of_name_key,
                 WHERE_NAME)
    return name if _rank(name) < _rank(about) else about


def chunk_cover(file: FileKeys, concept: Concept, chunk_id: ChunkId) -> Optional[Cover]:
    """The concept's strongest cover in one chunk: its word keys (passage words, or a skeleton: translation), or
    the same chunk's hint keys (translation; preferred over a skeleton, since it carries its grounded evidence)."""
    words = _best(concept.word, sorted(file.chunk_keys.get(chunk_id, frozenset()) & set(concept.word_keys)),
                  source_class_of_word_key, WHERE_CHUNK)
    hint_keys = sorted(file.hint_keys.get(chunk_id, frozenset()) & set(concept.hint_keys))
    hint = Cover(concept.word, SourceClass.TRANSLATION, WHERE_HINT, tuple(hint_keys)) if hint_keys else None
    return hint if hint is not None and _rank(hint) <= _rank(words) else words


def _chunk_ids(file: FileKeys) -> List[ChunkId]:
    return sorted(set(file.chunk_keys) | set(file.hint_keys))


def chunks_needing_text(file: FileKeys, concepts: Sequence[Concept]) -> Tuple[ChunkId, ...]:
    """The chunks where two or more concepts are covered better than by the record: their text (and hint evidence)
    decides the minimum window."""
    records = [record_cover(file, c) for c in concepts]
    out = []
    for cid in _chunk_ids(file):
        better = sum(1 for c, r in zip(concepts, records) if _rank(chunk_cover(file, c, cid)) < _rank(r))
        if better >= 2:
            out.append(cid)
    return tuple(out)


def cover_spans(cover: Cover, text: str, items: Sequence[HintItem]) -> Tuple[Span, ...]:
    """Where a chunk cover's keys occur in the chunk text, or a hint cover's grounded evidence spans."""
    if cover.where == WHERE_CHUNK:
        return match_spans(text, cover.keys)
    wanted = set(cover.keys)
    return tuple(sorted({(i.evidence_start, i.evidence_end) for i in items
                         if i.evidence_start >= 0 and wanted & set(word_form_keys(i.text))}))


def min_window(span_lists: Sequence[Sequence[Span]]) -> Optional[int]:
    """The length of the shortest stretch holding one span of every list (None when a list is empty)."""
    if not span_lists or any(not spans for spans in span_lists):
        return None
    events = sorted((s, e, i) for i, spans in enumerate(span_lists) for s, e in spans)
    need = len(span_lists)
    best: Optional[int] = None
    # sliding window over events sorted by start; the window's end is the max end inside it
    counts: Dict[int, int] = {}
    left = 0
    for right in range(len(events)):
        counts[events[right][2]] = counts.get(events[right][2], 0) + 1
        while len(counts) == need:
            window = events[left:right + 1]
            length = max(e for _, e, _ in window) - events[left][0]
            best = length if best is None else min(best, length)
            i = events[left][2]
            counts[i] -= 1
            if counts[i] == 0:
                del counts[i]
            left += 1
    return best


def _near(covers: Sequence[Cover], text: Optional[str], items: Sequence[HintItem]) -> bool:
    if len(covers) < 2:
        return True
    if text is None:
        return False
    window = min_window([cover_spans(c, text, items) for c in covers])
    return window is not None and window <= PROXIMITY_CHARS


def _missable(word: str, allowance: Allowance) -> Optional[str]:
    if word in allowance.common:
        return MISSED_COMMON
    if word == allowance.low_weight:
        return MISSED_LOW_WEIGHT
    return None


def _score(match: FileMatch) -> Tuple[int, int, int]:
    """Fewest misses, then the best tier, then the strongest covers overall (a Labelled file whose word is also a
    grounded passage keyword shows that evidence)."""
    return (len(match.missed), list(TIER_OF_RANK.values()).index(match.tier),
            sum(CLASS_RANK[c.source_class] for c in match.covers))


def _assign(file: FileKeys, concepts: Sequence[Concept], allowance: Allowance, records: Sequence[Optional[Cover]],
            cid: Optional[ChunkId], texts: Mapping[ChunkId, str],
            items: Mapping[ChunkId, Sequence[HintItem]]) -> Optional[FileMatch]:
    chunked = [chunk_cover(file, c, cid) if cid is not None else None for c in concepts]
    mandatory: List[int] = []
    optional: List[int] = []
    for i, (r, q) in enumerate(zip(records, chunked)):
        if q is not None and _rank(q) < _rank(r):
            (optional if r is not None or _missable(concepts[i].word, allowance) else mandatory).append(i)
    if len(optional) > _SUBSET_SEARCH_MAX:
        subsets = [tuple(optional), ()]
    else:
        subsets = [s for n in range(len(optional), -1, -1) for s in combinations(optional, n)]
    text = texts.get(cid) if cid is not None else None
    hint_items = items.get(cid, ()) if cid is not None else ()
    best: Optional[FileMatch] = None
    for kept in subsets:
        in_chunk = set(mandatory) | set(kept)
        covers: List[Cover] = []
        missed: List[Tuple[str, str]] = []
        feasible = True
        for i, concept in enumerate(concepts):
            cover = chunked[i] if i in in_chunk else records[i]
            if cover is not None:
                covers.append(cover)
                continue
            reason = _missable(concept.word, allowance)
            if reason is None:
                feasible = False
                break
            missed.append((concept.word, reason))
        if not feasible or not covers:
            continue
        if sum(1 for _, r in missed if r == MISSED_LOW_WEIGHT) > 1:
            continue
        if not _near([c for c in (chunked[i] for i in sorted(in_chunk)) if c is not None], text, hint_items):
            continue
        tier = TIER_OF_RANK[max(CLASS_RANK[c.source_class] for c in covers)]
        match = FileMatch(file.item_id, tier, tuple(covers), tuple(missed), cid if in_chunk else None)
        if best is None or _score(match) < _score(best):
            best = match
    return best


def classify(file: FileKeys, concepts: Sequence[Concept], allowance: Allowance, texts: Mapping[ChunkId, str],
             items: Mapping[ChunkId, Sequence[HintItem]]) -> Optional[FileMatch]:
    """The file's best tier among About, Mentioned in, By translation and Labelled, or None (strict AND failed, or a
    concept is absent). `texts` and `items` hold the chunk text and hint items of `chunks_needing_text`: a chunk
    whose text is missing fails proximity. Fewest misses first, then the best tier, then the strongest covers; a full
    tie keeps record-only covers."""
    if allowance.absent or not concepts:
        return None
    records = [record_cover(file, c) for c in concepts]
    best: Optional[FileMatch] = None
    for cid in [None, *_chunk_ids(file)]:
        match = _assign(file, concepts, allowance, records, cid, texts, items)
        if match is not None and (best is None or _score(match) < _score(best)):
            best = match
    return best


# -- meaning (§25.5.3) ------------------------------------------------------------------------------------------------

def source_scores(sims: Mapping[str, float]) -> Optional[Dict[str, float]]:
    """One source's robust z per file over its pool (the best MEANING_POOL visible files of its kNN page, similarity
    `1 - d`), or None when the pool has fewer than MEANING_POOL_MIN files (the source abstains)."""
    pool = sorted(sims.items(), key=lambda kv: (-kv[1], kv[0]))[:MEANING_POOL]
    if len(pool) < MEANING_POOL_MIN:
        return None
    values = [s for _, s in pool]
    mid = median(values)
    mad = median(abs(s - mid) for s in values)
    spread = max(1.4826 * mad, MIN_SPREAD)
    return {item: (s - mid) / spread for item, s in pool}


def long_doc_correction(chunks: int) -> float:
    """`√(2 ln n) − √(2 ln 4)` for a file of n > 4 readable chunks: the expected max of n draws grows with n."""
    if chunks <= 4:
        return 0.0
    return math.sqrt(2 * math.log(chunks)) - math.sqrt(2 * math.log(4))


def meaning_scores(card: Mapping[str, float], chunk: Mapping[str, float], hint: Mapping[str, float], *,
                   chunk_counts: Mapping[str, int], no_chunk_score: FrozenSet[str]) -> Dict[str, float]:
    """`meaning(file)`: the max z over the sources that did not abstain, chunk and hint z reduced by the long-document
    correction; files in `no_chunk_score` (EPUBs, notes under PROFILE_MIN_CHARS) give no chunk or hint score."""
    out: Dict[str, float] = dict(card)
    for source in (chunk, hint):
        for item, z in source.items():
            if item in no_chunk_score:
                continue
            value = z - long_doc_correction(chunk_counts.get(item, 0))
            out[item] = max(out.get(item, value), value)
    return out


def size_band(carded: int) -> int:
    """The drive-size band (§25.7): 0 under 200 carded files, 1 to 2 000, 2 to 10 000, 3 above."""
    return sum(1 for edge in DRIVE_SIZE_BANDS if carded >= edge)


def related_floor(band: int) -> float:
    """`Z_RELATED` for a band: one value until the scale test calibrates each (§25.7)."""
    return Z_RELATED
