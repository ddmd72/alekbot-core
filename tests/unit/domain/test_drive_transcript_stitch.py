"""Rebase, seam cut, speaker alignment and interpolation (RFC §27.6, Δ107-Δ109). Synthetic text only."""
import pytest

from src.domain.drive_index_params import STITCH_MIN_MATCHED_WORDS, TRANSCRIBE_MAX_GAP_S
from src.domain.drive_transcript import Segment, WindowSpec, WindowTranscript
from src.domain.drive_transcript_stitch import align_speakers, cut_seams, interpolate, rebase, seam_cuts
from src.domain.drive_transcript_windows import plan_windows


def _seg(start, text="x", speaker=None, **kw):
    return Segment(start_s=float(start), speaker=speaker, text=text, **kw)


def _wt(segments, speakers=2):
    return WindowTranscript(language_codes=("en",), speakers=speakers, no_speech=False, segments=tuple(segments))


def _line(n):
    """Six distinct synthetic words per script line."""
    return " ".join(f"{w}{n}" for w in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"))


# -- rebase / seams --------------------------------------------------------------------------------------------------

def test_rebase_adds_window_start():
    out = rebase(WindowSpec(1, 1740.0, 1800.0), [_seg(5, "a", 1), _seg(65.5, "b", 2)])
    assert [s.start_s for s in out] == [1745.0, 1805.5]
    assert [(s.speaker, s.text) for s in out] == [(1, "a"), (2, "b")]


def test_seam_cut_at_overlap_midpoint():
    specs = (WindowSpec(0, 0.0, 100.0), WindowSpec(1, 80.0, 100.0))   # overlap 80..100
    assert seam_cuts(specs) == (90.0,)
    k0 = [_seg(10, "a"), _seg(89, "b"), _seg(95, "dup-b")]
    k1 = [_seg(85, "dup-a"), _seg(90, "c"), _seg(150, "d")]
    out = cut_seams(specs, [k0, k1])
    assert [s.text for s in out] == ["a", "b", "c", "d"]


def test_crossing_segment_belongs_to_its_start():
    specs = (WindowSpec(0, 0.0, 100.0), WindowSpec(1, 80.0, 100.0))
    # a long segment starting at 89 (before the cut) stays in window 0 even though it runs past 90
    out = cut_seams(specs, [[_seg(89, "long, crossing the cut")], [_seg(91, "next")]])
    assert [s.text for s in out] == ["long, crossing the cut", "next"]


@pytest.mark.parametrize("overlap", [10, 30, 60, 90, 120])
def test_no_duplicate_and_no_loss_at_seams_property(overlap):
    script = [(7.0 * n + 1.0, n) for n in range(300)]          # 2 100 s of lines, one every 7 s
    specs = plan_windows(2100.0, window_s=600.0, overlap_s=float(overlap))
    assert len(specs) > 2
    windows = []
    for spec in specs:
        inside = [(t, n) for t, n in script if spec.start_s <= t < spec.start_s + spec.duration_s]
        windows.append((spec, _wt([_seg(t - spec.start_s, _line(n), 1 + (n + spec.index) % 2) for t, n in inside])))
    segments, uncertain, _ = align_speakers(windows)
    assert [s.text for s in segments] == [_line(n) for _, n in script]
    assert [s.start_s for s in segments] == sorted(s.start_s for s in segments)
    assert len(uncertain) == len(specs) - 1
    if overlap >= 30:      # speaker continuity: line n is always the same speaker, labels swapped per window
        assert [s.speaker for s in segments] == [1 + n % 2 for _, n in script]


# -- alignment -------------------------------------------------------------------------------------------------------

def _two_windows():
    """Overlap 60..120 on the file clock. Window 0 labels the speakers 1/2, window 1 swaps them."""
    w0 = WindowSpec(0, 0.0, 120.0)
    w1 = WindowSpec(1, 60.0, 120.0)
    k0 = [_seg(10, "opening words only here today", 1), _seg(70, _line(1), 1), _seg(85, _line(2), 2)]
    k1 = [_seg(10, _line(1), 2), _seg(25, _line(2), 1), _seg(100, "later talk by the first person", 2)]
    return [(w0, _wt(k0)), (w1, _wt(k1))]


def test_matched_labels_renamed():
    segments, uncertain, count = align_speakers(_two_windows())
    assert uncertain == (False,)
    assert count == 2
    by_text = {s.text: s.speaker for s in segments}
    assert by_text["later talk by the first person"] == 1     # window 1's label 2 is window 0's label 1
    assert by_text[_line(2)] == 2


def test_unmatched_label_gets_next_number():
    w0 = WindowSpec(0, 0.0, 120.0)
    w1 = WindowSpec(1, 60.0, 120.0)
    k0 = [_seg(70, _line(1), 1), _seg(85, _line(2), 2)]
    k1 = [_seg(10, _line(1), 1), _seg(25, _line(2), 2), _seg(100, "somebody new joins the talk", 3)]
    segments, _, count = align_speakers([(w0, _wt(k0)), (w1, _wt(k1, speakers=3))])
    assert segments[-1].speaker == 3
    assert count == 3


def test_speaker_silent_through_overlap_becomes_new():
    w0 = WindowSpec(0, 0.0, 120.0)
    w1 = WindowSpec(1, 60.0, 120.0)
    k0 = [_seg(10, "first speaker talks early", 1), _seg(20, "second speaker answers early", 2),
          _seg(70, _line(1), 1)]
    # the second speaker says nothing inside the overlap, then talks again in window 1 as its local "2"
    k1 = [_seg(10, _line(1), 1), _seg(80, "second speaker is back again now", 2)]
    segments, _, count = align_speakers([(w0, _wt(k0)), (w1, _wt(k1))])
    assert segments[-1].text.startswith("second speaker is back")
    assert segments[-1].speaker == 3          # over-states the count: the documented residual
    assert count == 3


def test_uncertain_seam_below_min_matched_words():
    w0 = WindowSpec(0, 0.0, 120.0)
    w1 = WindowSpec(1, 60.0, 120.0)
    few = "one two three"                      # 3 shared words < STITCH_MIN_MATCHED_WORDS
    assert 3 < STITCH_MIN_MATCHED_WORDS
    _, uncertain, _ = align_speakers([(w0, _wt([_seg(5, "other voice", 2), _seg(70, few, 1)])),
                                      (w1, _wt([_seg(10, few, 1)]))])
    assert uncertain == (True,)
    _, ok, _ = align_speakers([(w0, _wt([_seg(5, "other voice", 2), _seg(70, _line(1), 1)])),
                               (w1, _wt([_seg(10, _line(1), 1)]))])
    assert ok == (False,)


def test_single_window_never_uncertain():
    spec = WindowSpec(0, 0.0, 600.0)
    segments, uncertain, count = align_speakers([(spec, _wt([_seg(1, "hello there", 1), _seg(9, "hi", 2)]))])
    assert uncertain == ()
    assert count == 2
    assert [s.speaker for s in segments] == [1, 2]


def test_single_speaker_files_keep_none_labels():
    specs = plan_windows(300.0, window_s=200.0, overlap_s=60.0)
    windows = [(sp, _wt([_seg(5, _line(sp.index), None)], speakers=1)) for sp in specs]
    segments, _, count = align_speakers(windows)
    assert {s.speaker for s in segments} == {None}
    assert count == 1


def test_alignment_is_deterministic():
    first = align_speakers(_two_windows())
    assert align_speakers(_two_windows()) == first
    shuffled = [(spec, _wt(reversed(wt.segments))) for spec, wt in _two_windows()]
    assert align_speakers(shuffled) == first


def test_one_shared_function_word_does_not_map_a_speaker():
    w0 = WindowSpec(0, 0.0, 120.0)
    w1 = WindowSpec(1, 60.0, 120.0)
    k0 = [_seg(70, _line(1), 1), _seg(85, "the cat sat quietly", 2)]
    k1 = [_seg(10, _line(1), 1), _seg(35, "the dog barked loudly outside", 2)]
    segments, uncertain, count = align_speakers([(w0, _wt(k0)), (w1, _wt(k1))])
    assert count == 3                      # the "the" match is rejected: the second label of window 1 is new
    assert uncertain == (False,)           # the real match carries six words


def test_numbers_have_no_holes_and_count_matches():
    w0 = WindowSpec(0, 0.0, 120.0)
    w1 = WindowSpec(1, 60.0, 120.0)
    k0 = [_seg(10, "early talk", 1), _seg(70, _line(1), 1)]
    k1 = [_seg(10, _line(1), 1), _seg(100, "somebody else speaks late", 5)]
    segments, _, count = align_speakers([(w0, _wt(k0)), (w1, _wt(k1, speakers=5))])
    assert sorted({s.speaker for s in segments}) == list(range(1, count + 1))


def test_unlabelled_multi_window_seams_are_not_uncertain():
    specs = plan_windows(400.0, window_s=200.0, overlap_s=60.0)
    windows = [(sp, _wt([_seg(100, _line(sp.index), None)], speakers=1)) for sp in specs]
    _, uncertain, count = align_speakers(windows)
    assert count == 1 and uncertain == (False,) * (len(specs) - 1)


# -- interpolation ---------------------------------------------------------------------------------------------------

def _sentences(n, chars=60):
    return " ".join(("word " * (chars // 5 - 1)).strip() + "." for _ in range(n))


def test_gap_over_75s_gets_tilde_stamps():
    assert TRANSCRIBE_MAX_GAP_S == 75
    long_text = _sentences(20)                             # one segment, 300 s until the next
    segs = [_seg(0, long_text, 1), _seg(300, "closing line.", 1)]
    out, _ = interpolate(segs)
    assert len(out) > 2
    assert out[0].start_s == 0 and not out[0].interpolated
    inserted = out[1:-1]
    assert inserted and all(s.interpolated for s in inserted)
    starts = [s.start_s for s in out]
    assert starts == sorted(starts)
    assert all(b - a <= TRANSCRIBE_MAX_GAP_S for a, b in zip(starts, starts[1:]))
    assert " ".join(s.text for s in out[:-1]) == long_text
    assert out[-1].text == "closing line." and not out[-1].interpolated
    assert all(s.speaker == 1 for s in out)


def test_no_interpolation_below_gap():
    segs = [_seg(0, _sentences(4), 1), _seg(70, _sentences(4), 1), _seg(140, "end.", 1)]
    out, shares = interpolate(segs, (WindowSpec(0, 0.0, 200.0),))
    assert out == tuple(segs)
    assert shares == (0.0,)


def test_interpolated_share_reported():
    specs = (WindowSpec(0, 0.0, 400.0), WindowSpec(1, 340.0, 400.0))
    w0 = [_seg(0, _sentences(20), 1), _seg(300, "closing line of the first part.", 1)]
    w1 = [_seg(400, "plain.", 1), _seg(450, "plain too.", 1)]
    out, shares = interpolate(w0 + w1, specs)
    assert len(shares) == 2
    assert 0.0 < shares[0] < 1.0
    assert shares[1] == 0.0
    assert shares[0] == pytest.approx(sum(1 for s in out[:-2] if s.interpolated) / len(out[:-2]))


def test_a_short_segment_followed_by_a_long_other_speaker_is_not_split():
    segs = [_seg(0, "short reply.", 1), _seg(10, _sentences(30), 2), _seg(400, "back.", 1)]
    out, _ = interpolate(segs)
    assert out[0].text == "short reply."
    assert out[0] == segs[0]
