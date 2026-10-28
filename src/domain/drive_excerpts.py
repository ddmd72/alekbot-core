"""
Passages: ranking, selection, merging and display text (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A4, A6, A7,
A9). Pure: the service reads, this module decides.

Public interface:
    rank_excerpts(knn, keys, concepts) -> [ChunkId]        A4: RRF of the vector rank and a lexical rank
    select_excerpts(ranked, sizes, *, shown, hashes, noted) -> Selection     A6
    merge_runs(chunks) -> [ExcerptRun]                     A7: adjacent chunks merged, overlap removed
    display_text(text, kind) -> str                        A9: markdown escapes undone for MARKDOWN_ESCAPED_KINDS
    MARKDOWN_ESCAPED_KINDS                                 the kinds markitdown converts (plan D-P5)
"""
from __future__ import annotations

import re
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from .drive_excerpt_chunk import ExcerptChunk, display_number
from .drive_excerpt_run import ExcerptRun
from .drive_excerpt_selection import Selection
from .drive_index_lean import ChunkId
from .drive_index_params import (
    EXCERPTS_CHARS_MAX, EXCERPTS_K, EXCERPTS_LEXICAL_WINDOW, EXCERPTS_PER_FILE_MAX, EXCERPTS_RRF_K,
)
from .drive_match import Concept

# The keys of `adapters/drive_parser/readers.MARKITDOWN_CONVERTERS`; a unit test keeps the two equal (plan D-P5).
MARKDOWN_ESCAPED_KINDS: FrozenSet[str] = frozenset({"docx", "pptx", "epub", "msg"})

# The joint where a chunk's offsets disagree with its neighbour's text (A7).
GAP_JOINT = " … "

_MARKDOWN_ESCAPE = re.compile(r"\\([_*\[\]#])")


# -- A4 ranking ---------------------------------------------------------------------------------------------------

def _lexical_ranks(scores: Mapping[ChunkId, int]) -> Dict[ChunkId, float]:
    """Rank 1 = most concepts held; tied scores share the average of the ranks they span."""
    ordered = sorted(scores.values(), reverse=True)
    first: Dict[int, int] = {}
    count: Dict[int, int] = {}
    for position, score in enumerate(ordered, start=1):
        first.setdefault(score, position)
        count[score] = count.get(score, 0) + 1
    return {cid: first[s] + (count[s] - 1) / 2 for cid, s in scores.items()}


def rank_excerpts(knn: Sequence[Tuple[ChunkId, float]], keys: Mapping[ChunkId, Sequence[str]],
                  concepts: Sequence[Concept]) -> List[ChunkId]:
    """`knn`: (chunk, cosine distance); `keys`: the chunks' word keys (only the vector-nearest
    EXCERPTS_LEXICAL_WINDOW vote). A chunk holds a concept when it holds any of its `word_keys`; a chunk holding none
    gets no lexical rank, so across languages the vector order stands. No cosine floor (§10.1 step 4)."""
    by_vector = list(dict.fromkeys(cid for cid, _ in sorted(knn, key=lambda hit: hit[1])))
    vector_rank = {cid: rank for rank, cid in enumerate(by_vector, start=1)}
    wanted = [frozenset(c.word_keys) for c in concepts]
    scores: Dict[ChunkId, int] = {}
    for cid in by_vector[:EXCERPTS_LEXICAL_WINDOW]:
        held = set(keys.get(cid, ()))
        score = sum(1 for words in wanted if words & held)
        if score:
            scores[cid] = score
    lexical = _lexical_ranks(scores)

    def fused(cid: ChunkId) -> float:
        # Σ 1/(k + rank) as in src/domain/rrf.py, over a fractional lexical rank (plan D-P3)
        total = 1.0 / (EXCERPTS_RRF_K + vector_rank[cid])
        if cid in lexical:
            total += 1.0 / (EXCERPTS_RRF_K + lexical[cid])
        return total

    return sorted(by_vector, key=lambda cid: (-fused(cid), vector_rank[cid]))


# -- A6 selection -------------------------------------------------------------------------------------------------

def _keepers(ranked: Sequence[ChunkId], hashes: Mapping[str, Optional[str]],
             noted: FrozenSet[str]) -> Dict[str, Tuple[str, int]]:
    """item id -> (keeper item id, keeper generation) for each item with an identical copy among the ranked: the
    first noted copy, else the first ranked one (search's `_collapse` rule)."""
    first_gen: Dict[str, int] = {}
    for item_id, generation, _ in ranked:
        first_gen.setdefault(item_id, generation)
    keeper: Dict[str, str] = {}
    for item_id in first_gen:
        digest = hashes.get(item_id)
        if not digest:
            continue
        held = keeper.get(digest)
        if held is None or (item_id in noted and held not in noted):
            keeper[digest] = item_id
    out: Dict[str, Tuple[str, int]] = {}
    for item_id in first_gen:
        digest = hashes.get(item_id)
        if digest:
            chosen = keeper[digest]
            out[item_id] = (chosen, first_gen[chosen])
    return out


def select_excerpts(ranked: Sequence[ChunkId], sizes: Mapping[ChunkId, int], *, shown: FrozenSet[ChunkId],
                    hashes: Mapping[str, Optional[str]], noted: FrozenSet[str] = frozenset()) -> Selection:
    """In rank order, whole new chunks: at most EXCERPTS_K, EXCERPTS_CHARS_MAX characters (a chunk that does not fit
    is skipped, a later one may fit) and EXCERPTS_PER_FILE_MAX per file. Identical copies (same non-empty
    `content_hash`) are one file: a copy's chunk is its keeper's chunk of the same index. `noted`: item ids carrying
    the owner's note (preferred as keeper, as search does). `sizes` holds every ranked chunk's character count."""
    keepers = _keepers(ranked, hashes, noted)
    chosen: List[ChunkId] = []
    taken: Set[ChunkId] = set()
    repeated: List[ChunkId] = []
    per_file: Dict[str, int] = {}
    also_at: Dict[str, List[str]] = {}
    chars = 0
    for cid in ranked:
        if len(chosen) >= EXCERPTS_K:
            break
        item_id, generation, index = cid
        keeper_id, keeper_gen = keepers.get(item_id, (item_id, generation))
        if keeper_id != item_id and item_id not in also_at.get(keeper_id, []):
            also_at.setdefault(keeper_id, []).append(item_id)
        key = (keeper_id, keeper_gen, index)
        if key in shown:
            if key not in repeated:
                repeated.append(key)
            continue
        if key in taken or per_file.get(keeper_id, 0) >= EXCERPTS_PER_FILE_MAX:
            continue
        size = sizes[cid]
        if chars + size > EXCERPTS_CHARS_MAX:
            continue
        chosen.append(key)
        taken.add(key)
        per_file[keeper_id] = per_file.get(keeper_id, 0) + 1
        chars += size
    return Selection(chunks=tuple(chosen), repeated=tuple(repeated),
                     also_at={k: tuple(v) for k, v in also_at.items()})


# -- A7 merging ---------------------------------------------------------------------------------------------------

def _split(chunk: ExcerptChunk) -> Tuple[str, str, bool]:
    """(repeated header, source text, offsets agree with the text). The chunker stores a continued table's header
    before `source[char_start:char_end]`, so any extra length is that header."""
    extra = len(chunk.text) - (chunk.char_end - chunk.char_start)
    if extra < 0:
        return "", chunk.text, False
    return chunk.text[:extra], chunk.text[extra:], True


def _join(run_text: str, prev: ExcerptChunk, nxt: ExcerptChunk) -> str:
    _, prev_source, prev_ok = _split(prev)
    _, next_source, next_ok = _split(nxt)
    overlap = prev.char_end - nxt.char_start
    if not (prev_ok and next_ok):
        return run_text + GAP_JOINT + next_source
    if overlap <= 0:
        # the whitespace between the two spans is not stored: a line break stands for it
        return run_text + ("" if overlap == 0 else "\n") + next_source
    if overlap > len(next_source) or overlap > len(prev_source) or prev_source[-overlap:] != next_source[:overlap]:
        return run_text + GAP_JOINT + next_source
    return run_text + next_source[overlap:]


def merge_runs(chunks: Sequence[ExcerptChunk]) -> List[ExcerptRun]:
    """Per file (first-seen order), chunks in index order; adjacent indexes merge into one run. A repeated table
    header is kept once, at the run's head; text is never cut where offsets and text disagree (joined with
    GAP_JOINT instead)."""
    files: Dict[Tuple[str, int], Dict[int, ExcerptChunk]] = {}
    for chunk in chunks:
        files.setdefault((chunk.item_id, chunk.generation), {}).setdefault(chunk.index, chunk)
    runs: List[ExcerptRun] = []
    for (item_id, _), by_index in files.items():
        ordered = [by_index[i] for i in sorted(by_index)]
        start = ordered[0]
        text = start.text
        prev = start
        for chunk in ordered[1:]:
            if chunk.index == prev.index + 1:
                text = _join(text, prev, chunk)
            else:
                runs.append(ExcerptRun(item_id, display_number(start.index), display_number(prev.index), text))
                start, text = chunk, chunk.text
            prev = chunk
        runs.append(ExcerptRun(item_id, display_number(start.index), display_number(prev.index), text))
    return runs


# -- A9 display -----------------------------------------------------------------------------------------------------

def display_text(text: str, kind: str) -> str:
    """At render, not at index time: keys stay as indexed and no EXTRACTION_VERSION bump re-reads every file."""
    if kind not in MARKDOWN_ESCAPED_KINDS:
        return text
    return _MARKDOWN_ESCAPE.sub(r"\1", text)
