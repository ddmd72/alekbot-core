"""
Drive hints — grounding of passage hints (doc2query) against their chunk, and the reuse matching of an edited file's
chunks (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.4.2, §25.4.7). Pure: no I/O.

Public interface:
    GroundDrops(evidence, numbers, plain, invalid, far)        per-rule counts (telemetry `hints_dropped_*`)
    Grounded(items, hint_keys, drops)
    ground(chunk_text, items, *, chunk_word_keys=None, keyword_keys=None) -> Grounded
    drop_if_far(grounded, similarity) -> Grounded             rule 4, PASSAGE_MIN_SIMILARITY
    chunk_text_hash(text) -> str;  paragraph_hashes(text) -> (hash, ...)
    OldChunk(index, text_hash, paragraphs);  old_chunk(index, text) -> OldChunk
    reuse_source(new_text, old_chunks) -> Optional[int]        the old chunk whose hints a new chunk takes

Grounding (the Doc2query-- idea, Gospodinov et al., ECIR 2023, with evidence anchoring in place of a learned relevance
model):
1. the evidence quote must occur in the chunk after NFC, casefold and whitespace collapse (surrounding quotes and
   ellipses are not part of a quote); its span in the chunk text as stored is recorded;
2. every number, date or id in the item (its `drive_content_terms` maximal forms) must be among the chunk's number
   terms — the normaliser maps "13 мая 2026" and "13 de mayo de 2026" to one date, so a term written in another
   language or script is the same verified term;
3. a keyword whose forms are all among the chunk's `word_keys` adds no hint keys (a plain mention); hint keys are the
   other keywords' `w:` forms (`keyword_keys`, P1's `drive_keys.word_form_keys`), at most HINT_KEYS_PER_CHUNK,
   earliest first; questions add none;
4. a set whose hint vector is further from the chunk's vector than PASSAGE_MIN_SIMILARITY is dropped whole
   (`drop_if_far`; the vectors are the caller's).
Grounding proves the quote is in the passage, not that the keyword translates it: a hint match is automatic. At most
HINT_KEYWORDS_MAX keywords and HINT_QUESTIONS_MAX questions are kept, the first of each; a repeated item is dropped.
Reused items (§25.4.7) are grounded again against the new text, so their spans are recomputed.
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .drive_content_terms import content_terms, query_terms
from .drive_enrichment import HintItem, HintType
from .drive_index_params import (
    HINT_KEYS_PER_CHUNK, HINT_KEYWORDS_MAX, HINT_QUESTIONS_MAX, PASSAGE_MIN_SIMILARITY, PASSAGE_REUSE_SHARE,
)

KeywordKeys = Callable[[str], Sequence[str]]

_QUOTE_EDGES = "\"'“”„«»‘’‚‹›`…. "
_CAPS = {HintType.KEYWORD.value: HINT_KEYWORDS_MAX, HintType.QUESTION.value: HINT_QUESTIONS_MAX}


@dataclass(frozen=True)
class GroundDrops:
    evidence: int = 0     # rule 1: the quote is not in the chunk
    numbers: int = 0      # rule 2: a number, date or id not among the chunk's terms
    plain: int = 0        # rule 3: a keyword the chunk already holds (kept, no hint keys)
    invalid: int = 0      # an unknown type, an empty text, a repeat, over the caps
    far: int = 0          # rule 4: the whole set (1 per set dropped)


@dataclass(frozen=True)
class Grounded:
    items: Tuple[HintItem, ...]
    hint_keys: Tuple[str, ...]
    drops: GroundDrops


def ground(chunk_text: str, items: Sequence[HintItem], *, chunk_word_keys: Optional[Sequence[str]] = None,
           keyword_keys: Optional[KeywordKeys] = None) -> Grounded:
    """Rules 1-3 over one chunk's items. Without `keyword_keys` no hint keys are made (the caller re-keys)."""
    norm, positions = _normalised(chunk_text)
    terms = set(content_terms(chunk_text))
    held: Set[str] = set(chunk_word_keys or ())
    kept: List[HintItem] = []
    counts: Dict[str, int] = {}
    seen: Set[Tuple[str, str]] = set()
    evidence_drops = number_drops = plain = invalid = 0
    hint_keys: List[str] = []
    for item in items:
        text = " ".join(item.text.split())
        if item.type not in _CAPS or not text or (item.type, text.casefold()) in seen:
            invalid += 1
            continue
        span = _span(item.evidence, norm, positions)
        if span is None:
            evidence_drops += 1
            continue
        if any(term not in terms for term in query_terms(text)):
            number_drops += 1
            continue
        if counts.get(item.type, 0) >= _CAPS[item.type]:
            invalid += 1
            continue
        seen.add((item.type, text.casefold()))
        counts[item.type] = counts.get(item.type, 0) + 1
        kept.append(HintItem(text=text, type=item.type, evidence=chunk_text[span[0]:span[1]],
                             evidence_start=span[0], evidence_end=span[1]))
        if item.type == HintType.KEYWORD.value and keyword_keys is not None:
            forms = [k for k in dict.fromkeys(keyword_keys(text)) if k]
            if forms and all(k in held for k in forms):
                plain += 1
                continue
            hint_keys.extend(k for k in forms if k not in held)
    unique_keys = tuple(dict.fromkeys(hint_keys))[:HINT_KEYS_PER_CHUNK]
    return Grounded(items=tuple(kept), hint_keys=unique_keys,
                    drops=GroundDrops(evidence=evidence_drops, numbers=number_drops, plain=plain, invalid=invalid))


def drop_if_far(grounded: Grounded, similarity: float) -> Grounded:
    """Rule 4: the whole set goes when its hint vector's similarity to the chunk's vector is under
    PASSAGE_MIN_SIMILARITY; otherwise `grounded` itself."""
    if similarity >= PASSAGE_MIN_SIMILARITY or not grounded.items:
        return grounded
    return Grounded(items=(), hint_keys=(), drops=replace(grounded.drops, far=grounded.drops.far + 1))


# -- reuse (§25.4.7) ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OldChunk:
    """A chunk of the readable generation whose hint items a new chunk may take."""
    index: int
    text_hash: str
    paragraphs: Tuple[str, ...]


def chunk_text_hash(text: str) -> str:
    """`DriveHintDoc.chunk_text_hash`: the chunk text, NFC."""
    return hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8")).hexdigest()[:32]


def paragraph_hashes(text: str) -> Tuple[str, ...]:
    """One hash per non-empty paragraph (blank-line separated), whitespace collapsed."""
    out: List[str] = []
    for para in unicodedata.normalize("NFC", text).split("\n\n"):
        flat = " ".join(para.split())
        if flat:
            out.append(hashlib.sha256(flat.encode("utf-8")).hexdigest()[:16])
    return tuple(out)


def old_chunk(index: int, text: str) -> OldChunk:
    return OldChunk(index=index, text_hash=chunk_text_hash(text), paragraphs=paragraph_hashes(text))


def reuse_source(new_text: str, old_chunks: Sequence[OldChunk]) -> Optional[int]:
    """The old chunk whose hint items the new chunk takes: the same text hash, else the one chunk that gave at least
    PASSAGE_REUSE_SHARE of the new chunk's paragraphs (by paragraph hash; the highest share, then the lowest
    index). None: the chunk waits for passage enrichment."""
    digest = chunk_text_hash(new_text)
    exact = [c.index for c in old_chunks if c.text_hash == digest]
    if exact:
        return min(exact)
    paras = paragraph_hashes(new_text)
    if not paras:
        return None
    best: Optional[Tuple[float, int]] = None
    for chunk in old_chunks:
        have = set(chunk.paragraphs)
        share = sum(1 for p in paras if p in have) / len(paras)
        if share >= PASSAGE_REUSE_SHARE and (best is None or (share, -chunk.index) > (best[0], -best[1])):
            best = (share, chunk.index)
    return best[1] if best is not None else None


# -- implementation -------------------------------------------------------------------------------------------------

def _normalised(text: str) -> Tuple[str, List[int]]:
    """`text` decomposed (canonically equivalent to NFC for matching), casefolded, whitespace runs as one space, with
    the index in `text` each normalised character came from."""
    out: List[str] = []
    positions: List[int] = []
    in_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not in_space:
                out.append(" ")
                positions.append(i)
            in_space = True
            continue
        in_space = False
        for piece in unicodedata.normalize("NFD", ch).casefold():
            out.append(piece)
            positions.append(i)
    return "".join(out), positions


def _span(evidence: str, norm: str, positions: List[int]) -> Optional[Tuple[int, int]]:
    needle = " ".join(unicodedata.normalize("NFD", evidence).casefold().split()).strip(_QUOTE_EDGES)
    needle = unicodedata.normalize("NFD", needle)
    if not needle:
        return None
    at = norm.find(needle)
    if at < 0:
        return None
    return positions[at], positions[at + len(needle) - 1] + 1
