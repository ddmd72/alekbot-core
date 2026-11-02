"""
Merging the searches of several drive connections into one result (MULTI_DRIVE_RFC §11.2, §11.4, E9). Pure.

One part per searched connection, each already a complete result of its own index. The merge keeps the single-drive
contract: cards in (tier rank, mount order, own rank) order, each carrying the tag of the connection it was read from;
tier totals summed; the coverage facts combined so that anything one part could not see stays named; a part that
could not be searched (indexing off, disconnected, a folder it does not hold) is named in `not_searched_drives`
instead of failing the whole search. Identical content on two connections (equal `content_hash`, same family) is one
card with the other copies' canonical paths in `also_on` (a different hash family is a different identity and is
shown twice, §11.4).

Mount order: the primary first, then the other mounts by name.

Public interface:
    merge_results(parts, *, not_searched) -> DriveSearchResult
    merge_excerpts(parts, *, request, not_searched, no_text_listed) -> ExcerptResult
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .drive_excerpt_chunk import ExcerptChunk
from .drive_excerpt_file import ExcerptFile
from .drive_excerpt_request import ExcerptRequest
from .drive_excerpt_result import ExcerptResult
from .drive_index_lean import ChunkId
from .drive_index_params import EXCERPTS_CHARS_MAX, EXCERPTS_K, EXCERPTS_PER_FILE_MAX, RELATED_MAX
from .drive_match import MatchTier
from .drive_mount import Mount
from .drive_mount_name_change import mount_name_key
from .drive_search import (
    STATUS_OK, DriveSearchCard, DriveSearchEnvelope, DriveSearchResult,
)
from .drive_tier_result import CoverageFacts, TieredResult, TierBlock

__all__ = ["merge_results", "merge_excerpts"]

_TIER_RANK: Dict[str, int] = {t.value: i + 1 for i, t in enumerate(
    (MatchTier.ABOUT, MatchTier.MENTIONED, MatchTier.TRANSLATED, MatchTier.LABELLED, MatchTier.RELATED))}
_NO_TIER = 0            # a filter-only listing's cards carry no tier
_EMPTY_FILE_SUFFIX = ":0"
_FAR = float("inf")

SearchPart = Tuple[Mount, DriveSearchResult]
ExcerptPart = Tuple[Mount, ExcerptResult, Mapping[str, float]]


def _mount_order(mount: Mount) -> Tuple[bool, str]:
    return (not mount.primary, mount_name_key(mount.name))


def _union(*groups: Iterable[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _rank(tier: str) -> int:
    return _TIER_RANK.get(tier, _NO_TIER)


def _copy_key(content_hash: Optional[str]) -> Optional[str]:
    """The key two copies share: a non-empty content identity that is not an empty file's (empty files are not
    copies of one another)."""
    return content_hash if content_hash and not content_hash.endswith(_EMPTY_FILE_SUFFIX) else None


# ---------------------------------------------------------------------------------------------- search

def merge_results(parts: Sequence[SearchPart], *, not_searched: Tuple[str, ...]) -> DriveSearchResult:
    """The merged result of `parts` (one per searched connection). A part whose status is not OK adds its mount name
    to `not_searched_drives`; when no part is OK the first part's result is returned as it is (its status is the
    line the user reads today). `not_searched`: the names of the live drives this search did not select."""
    ordered = sorted(parts, key=lambda p: _mount_order(p[0]))
    ok = [(m, r) for m, r in ordered if r.status == STATUS_OK]
    if not ok:
        return ordered[0][1]
    failed = tuple(m.name for m, r in ordered if r.status != STATUS_OK)
    missing = _union(not_searched, failed)
    if len(ok) == 1:
        only = ok[0][1]
        tiered = only.tiered
        if tiered is not None:
            tiered = replace(tiered, coverage=replace(tiered.coverage, not_searched_drives=missing))
        return replace(only, envelope=replace(only.envelope, not_searched_drives=missing), tiered=tiered)

    cards, dropped, shifts = _merge_cards(ok)
    envelope = _merge_envelope([r.envelope for _, r in ok], missing)
    tiered = _merge_tiered(ok, shifts, missing, envelope)
    first = ok[0][1]
    totals = [r.matched_total for _, r in ok if r.matched_total is not None]
    return DriveSearchResult(
        status=STATUS_OK, cards=cards, envelope=envelope, request=first.request,
        matched_total=max(sum(totals) - dropped, 0) if totals else None,
        matched_at_least=any(r.matched_at_least for _, r in ok),
        folder_path=next((r.folder_path for _, r in ok if r.folder_path), None),
        folder_lookup_truncated=any(r.folder_lookup_truncated for _, r in ok),
        folder_not_indexed=all(r.folder_not_indexed for _, r in ok),
        tiered=tiered, awaiting_rebuild=all(r.awaiting_rebuild for _, r in ok),
        ref_tag="")


def _merge_cards(ok: Sequence[SearchPart]) -> Tuple[Tuple[DriveSearchCard, ...], int, Dict[str, int]]:
    """(cards in order, how many copies were collapsed, per-tier change of the totals the collapse causes)."""
    entries: List[Tuple[int, int, DriveSearchCard]] = []        # (mount index, own index, card)
    for m_index, (_, result) in enumerate(ok):
        entries.extend((m_index, c_index, card) for c_index, card in enumerate(result.cards))
    groups: Dict[str, List[int]] = {}
    for position, (_, _, card) in enumerate(entries):
        key = _copy_key(card.content_hash)
        if key is not None:
            groups.setdefault(key, []).append(position)
    removed: Set[int] = set()
    shifts: Dict[str, int] = {}
    replaced: Dict[int, DriveSearchCard] = {}
    for positions in groups.values():
        if len({entries[p][0] for p in positions}) < 2:
            continue                                   # copies on one connection are the part's own `also_at`
        keeper_pos = positions[0]                      # entries are in mount order: the primary's copy comes first
        keeper = entries[keeper_pos][2]
        best = min((entries[p][2] for p in positions), key=lambda c: _rank(c.tier) or 99)
        others = tuple(entries[p][2].path for p in positions[1:])
        merged = replace(keeper, also_on=_union(keeper.also_on, others))
        if _rank(best.tier) < _rank(keeper.tier):
            merged = replace(merged, tier=best.tier, sources=best.sources, passage=best.passage,
                             not_matched=best.not_matched, similar_name=best.similar_name)
            shifts[keeper.tier] = shifts.get(keeper.tier, 0) - 1
            shifts[best.tier] = shifts.get(best.tier, 0) + 1
        replaced[keeper_pos] = merged
        for p in positions[1:]:
            removed.add(p)
            tier = entries[p][2].tier
            shifts[tier] = shifts.get(tier, 0) - 1
    kept = [(entries[p][0], entries[p][1], replaced.get(p, entries[p][2]))
            for p in range(len(entries)) if p not in removed]
    kept.sort(key=lambda e: (_rank(e[2].tier), e[0], e[1]))
    cards: List[DriveSearchCard] = []
    related = 0
    for _, _, card in kept:
        if card.tier == MatchTier.RELATED.value:
            related += 1
            if related > RELATED_MAX:
                continue
        cards.append(card)
    return tuple(cards), len(removed), shifts


def _merge_envelope(envelopes: Sequence[DriveSearchEnvelope], not_searched: Tuple[str, ...]) -> DriveSearchEnvelope:
    as_of: List[datetime] = [e.index_as_of for e in envelopes if e.index_as_of is not None]
    return DriveSearchEnvelope(
        enabled=any(e.enabled for e in envelopes), content_search=any(e.content_search for e in envelopes),
        building=any(e.building for e in envelopes), refreshing=any(e.refreshing for e in envelopes),
        reconnect_needed=any(e.reconnect_needed for e in envelopes), index_as_of=min(as_of) if as_of else None,
        unread=sum(e.unread for e in envelopes), not_read_yet=sum(e.not_read_yet for e in envelopes),
        reading=any(e.reading for e in envelopes), index_full=any(e.index_full for e in envelopes),
        inaccessible=sum(e.inaccessible for e in envelopes), not_searched_drives=not_searched)


def _merge_coverage(coverages: Sequence[CoverageFacts], envelope: DriveSearchEnvelope,
                    not_searched: Tuple[str, ...]) -> CoverageFacts:
    return CoverageFacts(
        not_described=sum(c.not_described for c in coverages), not_describable=sum(c.not_describable for c in coverages),
        no_passages=sum(c.no_passages for c in coverages), not_read_yet=sum(c.not_read_yet for c in coverages),
        unread=sum(c.unread for c in coverages), building=envelope.building or any(c.building for c in coverages),
        reading=any(c.reading for c in coverages), refreshing=any(c.refreshing for c in coverages),
        rekeying=any(c.rekeying for c in coverages), full_page=any(c.full_page for c in coverages),
        keys_cut=sum(c.keys_cut for c in coverages), cut=_union(*(c.cut for c in coverages)),
        names_only=any(c.names_only for c in coverages), rebuilding=any(c.rebuilding for c in coverages),
        unchecked=_union(*(c.unchecked for c in coverages)), inaccessible=sum(c.inaccessible for c in coverages),
        not_searched_drives=not_searched)


def _merge_tiered(ok: Sequence[SearchPart], shifts: Mapping[str, int], not_searched: Tuple[str, ...],
                  envelope: DriveSearchEnvelope) -> Optional[TieredResult]:
    tiereds = [r.tiered for _, r in ok if r.tiered is not None]
    if not tiereds:
        return None
    coverage = _merge_coverage([t.coverage for t in tiereds], envelope, not_searched)
    blocks: List[TierBlock] = []
    for tier in _TIER_RANK:
        held = [b for t in tiereds for b in t.blocks if b.tier.value == tier]
        total = sum(b.total for b in held) + shifts.get(tier, 0)
        if not held or total <= 0:
            continue
        blocks.append(TierBlock(tier=MatchTier(tier), total=total, at_least=any(b.at_least for b in held),
                                rest=tuple(m for b in held for m in b.rest),
                                nonce=next((b.nonce for b in held if b.nonce), "")))
    absent = tuple(w for w in tiereds[0].absent if all(w in t.absent for t in tiereds[1:]))
    in_scope = tuple(w for w in _union(*(t.absent for t in tiereds), *(t.absent_in_scope for t in tiereds))
                     if w not in absent)
    withouts = [t.without for t in tiereds if t.without is not None]
    conjunction: Dict[str, int] = {}
    for tiered in tiereds:
        for word, count in tiered.conjunction:
            conjunction[word] = conjunction.get(word, 0) + count
    return TieredResult(
        concepts=tiereds[0].concepts, ignored=_union(*(t.ignored for t in tiereds)),
        common=_union(*(t.common for t in tiereds)), coverage=coverage, blocks=tuple(blocks),
        related_small=any(t.related_small for t in tiereds), absent=absent, absent_in_scope=in_scope,
        without=sum(withouts) if withouts else None, without_at_least=any(t.without_at_least for t in tiereds),
        conjunction=tuple(conjunction.items()), expired=sum(t.expired for t in tiereds))


# ---------------------------------------------------------------------------------------------- passages

def merge_excerpts(parts: Sequence[ExcerptPart], *, request: ExcerptRequest, not_searched: Tuple[str, ...],
                   no_text_listed: int) -> ExcerptResult:
    """The merged passages of `parts`: (mount, its result, the best chunk distance of each of its files by item id).
    Files are ordered by best distance (ties: mount order); identical content on two connections is one file (the
    primary's, else the first mount's) whose `also_at` names the other copies; the per-call caps (EXCERPTS_K chunks,
    EXCERPTS_CHARS_MAX characters, EXCERPTS_PER_FILE_MAX per file) are applied after the merge. A part that could not
    be read adds its mount name to `not_searched_drives`; when none could, the first part's result stands."""
    ordered = sorted(parts, key=lambda p: _mount_order(p[0]))
    def readable(part: ExcerptPart) -> bool:
        return part[1].status == STATUS_OK and not (part[1].awaiting_rebuild or part[1].wrong_space)

    ok = [p for p in ordered if readable(p)]
    if not ok:
        return ordered[0][1]
    missing = _union(not_searched, (p[0].name for p in ordered if not readable(p)))
    files = _merge_files(ok)
    repeated, tag = _merge_repeated(ok, files)
    results = [r for _, r, _ in ok]
    context = results[0].context
    for result in results[1:]:
        context = context.merge(result.context)
    paths = tuple(p for r in results for p in r.no_text_paths)[:no_text_listed]
    return ExcerptResult(
        status=STATUS_OK, request=request, files=files, repeated=repeated, no_text=sum(r.no_text for r in results),
        no_text_paths=paths, building=any(r.building for r in results),
        folder_path=next((r.folder_path for r in results if r.folder_path), ""),
        folder_candidates=tuple(c for r in results for c in r.folder_candidates),
        folder_lookup_truncated=any(r.folder_lookup_truncated for r in results),
        folder_not_indexed=all(r.folder_not_indexed for r in results), context=context, ref_tag=tag,
        not_searched_drives=missing)


def _merge_files(ok: Sequence[ExcerptPart]) -> Tuple[ExcerptFile, ...]:
    entries: List[Tuple[float, int, ExcerptFile]] = []
    for m_index, (_, result, distances) in enumerate(ok):
        entries.extend((distances.get(f.item_id, _FAR), m_index, f) for f in result.files)
    groups: Dict[str, List[Tuple[float, int, ExcerptFile]]] = {}
    for entry in entries:
        key = _copy_key(entry[2].content_hash)
        if key is not None:
            groups.setdefault(key, []).append(entry)
    merged: List[Tuple[float, int, ExcerptFile]] = []
    for entry in entries:
        key = _copy_key(entry[2].content_hash)
        copies = groups.get(key, []) if key is not None else []
        if len({e[2].ref_tag for e in copies}) < 2:
            merged.append(entry)
            continue
        keeper = min(copies, key=lambda e: (e[1], e[0]))
        if entry is not keeper:
            continue
        file = keeper[2]
        for other in sorted(copies, key=lambda e: (e[0], e[1])):
            if other is not keeper:
                file = _absorb(file, other[2])
        merged.append((min(e[0] for e in copies), keeper[1], file))
    merged.sort(key=lambda e: (e[0], e[1]))
    return _capped([e[2] for e in merged])


def _absorb(keeper: ExcerptFile, copy: ExcerptFile) -> ExcerptFile:
    """The keeper with the copy's other-connection path and the chunks it lacks (same content, same index)."""
    have = {c.index for c in keeper.chunks}
    extra = tuple(replace(c, item_id=keeper.item_id, generation=keeper.generation)
                  for c in copy.chunks if c.index not in have)
    chunks = tuple(sorted((*keeper.chunks, *extra), key=lambda c: c.index))
    return replace(keeper, chunks=chunks, also_at=_union(keeper.also_at, (copy.path,), copy.also_at))


def _capped(files: Sequence[ExcerptFile]) -> Tuple[ExcerptFile, ...]:
    """The per-call caps over the merged files, in file order (a chunk that does not fit is skipped)."""
    out: List[ExcerptFile] = []
    count = 0
    chars = 0
    for file in files:
        kept: List[ExcerptChunk] = []
        for chunk in file.chunks:
            if count >= EXCERPTS_K or len(kept) >= EXCERPTS_PER_FILE_MAX or chars + len(chunk.text) > EXCERPTS_CHARS_MAX:
                continue
            kept.append(chunk)
            count += 1
            chars += len(chunk.text)
        if kept:
            out.append(replace(file, chunks=tuple(kept)))
    return tuple(out)


def _merge_repeated(ok: Sequence[ExcerptPart], files: Sequence[ExcerptFile]) -> Tuple[Tuple[ChunkId, ...], str]:
    """(repeated chunk ids, the one ref tag of the result). A repeated chunk id carries no tag, so it is kept only
    while every connection that contributed files or repeated chunks has the same tag (then the result's `ref_tag`
    is that tag); with several tags the repeated list is dropped rather than printed under a wrong connection."""
    tags = {f.ref_tag for f in files} | {m.ref_tag for m, r, _ in ok if r.repeated}
    tag = next(iter(tags)) if len(tags) == 1 else ""
    if len(tags) > 1:
        return (), ""
    return tuple(dict.fromkeys(c for _, r, _ in ok for c in r.repeated)), tag
