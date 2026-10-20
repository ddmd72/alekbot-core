"""Window plan, coverage check, time sanity and the content key (RFC §27.4-§27.6, Δ129, Δ135)."""
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_index_params import (
    TRANSCRIBE_COVERAGE_MIN, TRANSCRIBE_OVERLAP_S, TRANSCRIBE_WINDOW_S,
)
from src.domain.drive_transcript import Segment, WindowSpec
from src.domain.drive_transcript_windows import content_key, coverage_ok, plan_windows, times_ok

W, O = TRANSCRIBE_WINDOW_S, TRANSCRIBE_OVERLAP_S


def _seg(start, text="x"):
    return Segment(start_s=float(start), speaker=None, text=text)


def test_params_pinned():
    assert (W, O, TRANSCRIBE_COVERAGE_MIN) == (1800, 60, 0.9)


def test_boundaries_and_overlap():
    ws = plan_windows(5000)
    assert [w.start_s for w in ws] == [0, 1740, 3480]
    assert [w.index for w in ws] == [0, 1, 2]
    assert [w.duration_s for w in ws[:2]] == [1800, 1800]
    for a, b in zip(ws, ws[1:]):
        assert a.start_s + a.duration_s - b.start_s == O


def test_last_window_short():
    ws = plan_windows(5000)
    assert ws[-1].start_s + ws[-1].duration_s == 5000
    assert ws[-1].duration_s == 5000 - 3480


def test_short_file_one_window():
    assert plan_windows(600) == (WindowSpec(index=0, start_s=0.0, duration_s=600.0),)
    assert len(plan_windows(1800)) == 1


def test_no_zero_length_window():
    for d in (1, 1799, 1800, 1801, 1860, 3540, 3541, 3600, 14400):
        assert all(w.duration_s > 0 for w in plan_windows(d)), d
    assert plan_windows(0) == ()


def test_windows_cover_the_whole_file_property():
    for window_s, overlap_s in ((1800, 60), (2700, 60), (1800, 0)):
        for d in list(range(1, 400, 37)) + list(range(1700, 9000, 113)) + [14400]:
            ws = plan_windows(d, window_s, overlap_s)
            assert ws[0].start_s == 0
            assert ws[-1].start_s + ws[-1].duration_s == d
            for a, b in zip(ws, ws[1:]):
                assert b.start_s <= a.start_s + a.duration_s - overlap_s + 1e-9
                assert b.start_s > a.start_s
            assert all(0 < w.duration_s <= window_s for w in ws)


def test_45_min_window_allowed():
    ws = plan_windows(7200, 2700, 60)
    assert [w.start_s for w in ws] == [0, 2640, 5280]
    assert ws[0].duration_s == 2700


def test_short_answer_is_not_covered():
    w = WindowSpec(index=0, start_s=0, duration_s=1800)
    assert not coverage_ok(w, [_seg(0), _seg(600)], no_speech=False)
    assert not coverage_ok(w, [], no_speech=False)


def test_no_speech_window_always_covered():
    w = WindowSpec(index=0, start_s=0, duration_s=1800)
    assert coverage_ok(w, [], no_speech=True)
    assert coverage_ok(w, [_seg(3)], no_speech=True)


def test_exactly_at_threshold():
    w = WindowSpec(index=0, start_s=0, duration_s=100)
    assert coverage_ok(w, [_seg(0), _seg(90)], no_speech=False)
    assert not coverage_ok(w, [_seg(0), _seg(89.9)], no_speech=False)


def test_non_monotonic_beyond_5s_fails():
    w = WindowSpec(index=0, start_s=0, duration_s=1800)
    assert not times_ok(w, [_seg(100), _seg(94)])


def test_small_jitter_passes():
    w = WindowSpec(index=0, start_s=0, duration_s=1800)
    assert times_ok(w, [_seg(100), _seg(95), _seg(96)])
    assert times_ok(w, [])


def test_start_past_window_plus_10s_fails():
    w = WindowSpec(index=0, start_s=0, duration_s=1800)
    assert times_ok(w, [_seg(1810)])
    assert not times_ok(w, [_seg(1810.5)])


def _rec(**kw):
    return DriveIndexRecord(user_id="u1", item_id="it1", **kw)


def test_quickxor_key():
    rec = _rec(content_hash="qx==:1234", content_version="ctag1")
    assert content_key(rec) == ("qx==:1234", True)


def test_no_quickxor_falls_back_to_item_ctag_unshared():
    rec = _rec(content_hash=None, content_version="ctag1")
    assert content_key(rec) == ("item:it1:ctag1", False)
    assert content_key(_rec(content_hash="", content_version="c")) == ("item:it1:c", False)


def test_sha1_is_never_used():
    rec = _rec(content_hash=None, content_version="c", facets={"hashes": {"sha1Hash": "AB", "sha256Hash": "CD"}})
    assert content_key(rec) == ("item:it1:c", False)
