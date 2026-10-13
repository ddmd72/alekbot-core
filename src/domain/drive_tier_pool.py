"""
Drive tier pool — the per-concept hits of Search v2's streams, document frequency, candidates and the order inside a
tier (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.5.2, §25.5.4, §25.5.6). Pure: no I/O.

Public interface:
    ConceptHits                 one concept's hit files per stream (about, word chunks, hint documents, names)
    document_frequency(hits)    distinct files per concept, and whether a page was full (a lower bound)
    candidate_ids(concepts, in_scope, df, allowance, full) -> set   the files whose coverage is tested
    concept_trigrams(concept)   the name stream's trigram keys of a word: native and skeleton (similar names)
    name_word_concepts(query)   a query of generic words only, as name-word concepts (P6 review m6)
    tier_order_key(match, missed, meaning, effective_date, item_id)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .drive_index_lean import ChunkId
from .drive_key_forms import query_skeletons
from .drive_keys import GENERIC_WORDS, name_keys_v2, query_concepts
from .drive_match import Concept
from .drive_name_keys import _strip_accents, _trigrams
from .drive_tiers import Allowance

_EPOCH = datetime.min


@dataclass
class ConceptHits:
    """One concept's hits, fenced (a chunk or hint document of another generation is not a hit): records by about
    keys, chunks by word keys and hint documents by hint keys, grouped by file, and records whose whole-token name keys
    hold it (trigrams never count). `full` when any of its pages was full."""
    about: Set[str] = field(default_factory=set)
    words: Dict[str, Set[ChunkId]] = field(default_factory=dict)
    hints: Dict[str, Set[ChunkId]] = field(default_factory=dict)
    names: Set[str] = field(default_factory=set)
    full: bool = False
    unchecked: bool = False         # a full page no scoped read covered: absence in a filtered scope is unknown

    def files(self) -> Set[str]:
        return self.about | set(self.words) | set(self.hints) | self.names


def document_frequency(hits: Sequence[ConceptHits]) -> Tuple[List[int], List[bool]]:
    """`df` per concept (§25.5.2): distinct files in the union of its about, word, hint and name hits, user-wide,
    and whether it is a lower bound (a page was full)."""
    return [len(h.files()) for h in hits], [h.full for h in hits]


def candidate_ids(concepts: Sequence[Concept], in_scope: Sequence[Set[str]], df: Sequence[int],
                  allowance: Allowance, full: Sequence[bool] = ()) -> Set[str]:
    """The files whose coverage is tested (§25.5.2): every match covers each REQUIRED concept (neither common nor the
    low-weight one), so the in-scope hits of the rarest required concept hold them all — while its pages were
    complete. When its page was full (a slice by item id), the next required concept's in-scope hits join (P6 review
    m2). Without a required concept (every word missable), the union of all concepts' hits."""
    required = [i for i, c in enumerate(concepts)
                if c.word not in allowance.common and c.word != allowance.low_weight]
    if not required:
        return set().union(*in_scope) if in_scope else set()
    ranked = sorted(required, key=lambda i: (df[i], len(in_scope[i]), i))
    out = set(in_scope[ranked[0]])
    if len(ranked) > 1 and full and full[ranked[0]]:
        out |= in_scope[ranked[1]]
    return out


def concept_trigrams(concept: Concept) -> Tuple[str, ...]:
    """`t:` keys of a word and of its skeletons (a Cyrillic misspelling of a Latin name shares the skeleton's
    trigrams, P6 review m4); a number has none. Trigrams never cover: they find similar names."""
    if concept.is_number:
        return ()
    grams = list(_trigrams(_strip_accents(concept.word)))
    for skeleton in query_skeletons(concept.word):
        grams += _trigrams(skeleton)
    return tuple(dict.fromkeys("t:" + g for g in grams))


def name_word_concepts(query: str) -> Tuple[Concept, ...]:
    """The generic words of a query that has no concept ("todo", "files"), each as a name-word concept: its name keys
    v2 without trigrams. Searched by name alone (P6 review m6)."""
    out = []
    for word in query_concepts(query).ignored:
        if word in GENERIC_WORDS or _strip_accents(word) in GENERIC_WORDS:
            keys = tuple(k for k in name_keys_v2(word) if not k.startswith("t:"))
            if keys:
                out.append(Concept(word=word, about_keys=(), word_keys=keys, hint_keys=(), is_number=False))
    return tuple(out)


def tier_order_key(missed: int, meaning: Optional[float], effective_date: Optional[datetime],
                   item_id: str) -> Tuple[int, float, float, str]:
    """Inside a tier: files missing no word first, then the meaning score, then newest, then item id."""
    when = effective_date.timestamp() if effective_date is not None else float("-inf")
    return missed, -(meaning if meaning is not None else float("-inf")), -when, item_id
