"""A6: whole new chunks in rank order, bounded per call and per file; identical copies are one file."""
from src.domain.drive_excerpts import select_excerpts
from src.domain.drive_index_params import EXCERPTS_CHARS_MAX, EXCERPTS_K, EXCERPTS_PER_FILE_MAX


def _ids(item_id, n, generation=1):
    return [(item_id, generation, i) for i in range(n)]


def _sizes(ids, size=500):
    return {cid: size for cid in ids}


def test_at_most_k_chunks_in_rank_order():
    ranked = _ids("item-1", 5) + _ids("item-2", 5) + _ids("item-3", 5)
    sel = select_excerpts(ranked, _sizes(ranked), shown=frozenset(), hashes={})
    assert len(sel.chunks) == EXCERPTS_K
    assert list(sel.chunks) == [c for c in ranked][:EXCERPTS_K]


def test_the_per_file_cap_lets_a_second_file_in():
    ranked = _ids("item-1", 12) + _ids("item-2", 3)
    sel = select_excerpts(ranked, _sizes(ranked), shown=frozenset(), hashes={})
    assert [c for c in sel.chunks if c[0] == "item-1"] == _ids("item-1", EXCERPTS_PER_FILE_MAX)
    assert [c for c in sel.chunks if c[0] == "item-2"] == _ids("item-2", 2)


def test_an_oversized_chunk_is_skipped_and_a_later_one_fits():
    ranked = [("item-1", 1, 0), ("item-2", 1, 0), ("item-3", 1, 0)]
    sizes = {ranked[0]: EXCERPTS_CHARS_MAX - 1_000, ranked[1]: 2_000, ranked[2]: 1_000}
    sel = select_excerpts(ranked, sizes, shown=frozenset(), hashes={})
    assert sel.chunks == (ranked[0], ranked[2])


def test_the_character_budget_is_inclusive():
    ranked = [("item-1", 1, 0), ("item-2", 1, 0)]
    sizes = {ranked[0]: EXCERPTS_CHARS_MAX // 2, ranked[1]: EXCERPTS_CHARS_MAX // 2}
    assert select_excerpts(ranked, sizes, shown=frozenset(), hashes={}).chunks == tuple(ranked)


def test_only_new_chunks_against_the_ledger_and_the_shown_ones_listed_as_repeated():
    ranked = _ids("item-1", 4)
    shown = frozenset({("item-1", 1, 0), ("item-1", 1, 2)})
    sel = select_excerpts(ranked, _sizes(ranked), shown=shown, hashes={})
    assert sel.chunks == (("item-1", 1, 1), ("item-1", 1, 3))
    assert sel.repeated == (("item-1", 1, 0), ("item-1", 1, 2))


def test_a_shown_chunk_does_not_use_up_the_k():
    ranked = _ids("item-1", 3) + _ids("item-2", 8) + _ids("item-3", 3)
    shown = frozenset(_ids("item-1", 3))
    sel = select_excerpts(ranked, _sizes(ranked), shown=shown, hashes={})
    assert len(sel.chunks) == EXCERPTS_K
    assert sel.repeated == tuple(_ids("item-1", 3))


def test_identical_copies_are_one_file_with_also_at():
    # item-2 is a byte-identical copy of item-1 (same content hash): its chunks are item-1's chunks
    ranked = [("item-1", 1, 0), ("item-2", 5, 0), ("item-2", 5, 1), ("item-3", 1, 0)]
    hashes = {"item-1": "sha:abc", "item-2": "sha:abc", "item-3": "sha:def"}
    sel = select_excerpts(ranked, _sizes(ranked), shown=frozenset(), hashes=hashes)
    assert sel.chunks == (("item-1", 1, 0), ("item-1", 1, 1), ("item-3", 1, 0))
    assert sel.also_at == {"item-1": ("item-2",)}


def test_an_empty_hash_never_collapses():
    ranked = [("item-1", 1, 0), ("item-2", 1, 0)]
    sel = select_excerpts(ranked, _sizes(ranked), shown=frozenset(), hashes={"item-1": "", "item-2": None})
    assert sel.chunks == tuple(ranked)
    assert sel.also_at == {}


def test_a_noted_copy_is_the_keeper():
    ranked = [("item-1", 1, 0), ("item-2", 5, 0), ("item-2", 5, 1)]
    hashes = {"item-1": "sha:abc", "item-2": "sha:abc"}
    sel = select_excerpts(ranked, _sizes(ranked), shown=frozenset(), hashes=hashes, noted=frozenset({"item-2"}))
    assert sel.chunks == (("item-2", 5, 0), ("item-2", 5, 1))
    assert sel.also_at == {"item-2": ("item-1",)}


def test_copies_share_the_per_file_cap():
    ranked = _ids("item-1", 5) + [("item-2", 1, i) for i in range(5, 10)]
    hashes = {"item-1": "sha:abc", "item-2": "sha:abc"}
    sel = select_excerpts(ranked, _sizes(ranked), shown=frozenset(), hashes=hashes)
    assert len(sel.chunks) == EXCERPTS_PER_FILE_MAX
    assert {c[0] for c in sel.chunks} == {"item-1"}
