"""The content half of drive search, pure (USER_DRIVE_INDEXING_RFC §7.4, §10.1 step 3, §10.2, §17.4, §17.6, §24.6):
the read fence, pooling per record before RRF, the term ranking, the window and `matched`."""
from dataclasses import replace

from src.domain.drive_content_search import (
    chunk_readable, matched_label, pool_nearest, pool_terms, window_indexes, window_text,
)
from src.domain.drive_index import DriveChunk, DriveIndexRecord, RecordState
from src.domain.drive_index_params import RRF_K
from src.domain.drive_search import MATCHED_BOTH, MATCHED_CARD, MATCHED_CONTENT
from src.domain.rrf import apply_rrf_ranking


def rec(item_id, gen=1, **kw):
    return DriveIndexRecord(user_id="u", item_id=item_id, kind="pdf", state=kw.pop("state", RecordState.INDEXED),
                            indexed_generation=gen, **kw)


def ch(item_id, index=0, gen=1, text="", start=None, terms=()):
    start = index * 100 if start is None else start
    return DriveChunk("u", item_id, gen, index, text, start, start + len(text), "pdf", tuple(terms))


class TestFence:
    def test_only_the_readable_generation_of_a_live_record(self):
        assert chunk_readable(ch("a", gen=2), rec("a", gen=2))
        assert not chunk_readable(ch("a", gen=1), rec("a", gen=2))      # re-index at the same cTag
        assert not chunk_readable(ch("a", gen=3), rec("a", gen=2))      # a crashed run's partial generation
        assert not chunk_readable(ch("a"), rec("a", gen=None))          # demoted: chunks invisible before collection
        assert not chunk_readable(ch("a"), rec("a", state=RecordState.DELETING))
        assert not chunk_readable(ch("a"), None)                        # the record is gone


class TestPoolNearest:
    def test_one_hit_per_record_its_nearest_readable_chunk(self):
        records = {"a": rec("a", gen=2), "b": rec("b")}
        hits = [(ch("a", 0, gen=1), 0.01),   # stale generation: fenced out although nearest
                (ch("a", 3, gen=2), 0.10), (ch("a", 1, gen=2), 0.05), (ch("b", 0), 0.07), (ch("c", 0), 0.0)]
        pooled = pool_nearest(hits, records)
        assert [(h.record.item_id, h.chunk.index, h.score) for h in pooled] == [("a", 1, 0.05), ("b", 0, 0.07)]

    def test_pooling_before_rrf_counts_a_many_chunk_record_once(self):
        """§17.6: unpooled, five chunks of `a` would outvote `b`, which two streams found."""
        a, b = rec("a"), rec("b")
        hits = [(ch("a", i), 0.01 * i) for i in range(5)] + [(ch("b", 0), 0.06)]
        card_stream = [b]
        pooled = [h.record for h in pool_nearest(hits, {"a": a, "b": b})]
        assert pooled == [a, b]
        assert [r.item_id for r in apply_rrf_ranking([card_stream, pooled], key_fn=lambda r: r.item_id,
                                                     k=RRF_K)] == ["b", "a"]
        unpooled = [rec(c.item_id) for c, _ in sorted(hits, key=lambda h: h[1])]
        assert [r.item_id for r in apply_rrf_ranking([card_stream, unpooled], key_fn=lambda r: r.item_id,
                                                     k=RRF_K)][0] == "a"   # the failure pooling prevents


class TestPoolTerms:
    def test_more_shared_terms_first_and_the_anchor_holds_most(self):
        records = {i: rec(i) for i in "abc"}
        terms = ("inv20240012", "20240012", "2024", "0012")
        hits = {
            "inv20240012": [ch("a", 2)],
            "20240012": [ch("a", 2)],
            "2024": [ch("a", 0), ch("a", 2), ch("b", 0), ch("c", 4)],
            "0012": [ch("a", 2), ch("c", 1)],
        }
        pooled = pool_terms(hits, records, terms)
        assert [(h.record.item_id, h.chunk.index, h.score) for h in pooled] == [("a", 2, 4.0), ("c", 1, 2.0),
                                                                               ("b", 0, 1.0)]

    def test_a_tie_goes_to_the_more_specific_term(self):
        records = {"a": rec("a"), "b": rec("b")}
        pooled = pool_terms({"1250.00": [ch("b")], "1250": [ch("a")]}, records, ("1250.00", "1250"))
        assert [h.record.item_id for h in pooled] == ["b", "a"]

    def test_fenced(self):
        records = {"a": rec("a", gen=2)}
        assert pool_terms({"1250": [ch("a", gen=1)]}, records, ("1250",)) == []
        assert pool_terms({"1250": [ch("z")]}, records, ("1250",)) == []

    def test_anchor_tie_takes_the_lowest_index(self):
        pooled = pool_terms({"1250": [ch("a", 5), ch("a", 2)]}, {"a": rec("a")}, ("1250",))
        assert pooled[0].chunk.index == 2


class TestWindow:
    def test_indexes(self):
        assert window_indexes(0) == (0, 1)
        assert window_indexes(4) == (3, 4, 5)

    def test_overlap_is_cut_by_offsets(self):
        full = "0123456789" * 3
        chunks = [replace(ch("a", 0, text=full[0:12]), char_start=0, char_end=12),
                  replace(ch("a", 1, text=full[10:22]), char_start=10, char_end=22),
                  replace(ch("a", 2, text=full[20:30]), char_start=20, char_end=30)]
        assert window_text(list(reversed(chunks))) == full

    def test_a_missing_neighbour_is_a_visible_gap(self):
        text = window_text([ch("a", 0, text="first"), ch("a", 2, text="third")])
        assert text.startswith("first") and text.endswith("third") and "[…]" in text

    def test_offsets_that_do_not_fit_are_never_cut_blindly(self):
        chunks = [replace(ch("a", 0, text="abc"), char_start=0, char_end=500),
                  replace(ch("a", 1, text="def"), char_start=3, char_end=6)]
        assert window_text(chunks).endswith("def") and "abc" in window_text(chunks)

    def test_empty(self):
        assert window_text([]) == ""


def test_matched_label():
    assert matched_label(True, False) == MATCHED_CARD
    assert matched_label(False, True) == MATCHED_CONTENT
    assert matched_label(True, True) == MATCHED_BOTH
    assert matched_label(False, False) == MATCHED_CARD
