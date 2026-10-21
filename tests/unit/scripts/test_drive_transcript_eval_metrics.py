"""scripts/validation/drive_eval/transcript_metrics.py (USER_DRIVE_INDEXING_RFC §27.14): the eval's metrics on synthetic
gold/transcript pairs. No network, no model."""
import pytest

from scripts.validation.drive_eval import transcript_metrics as m
from src.domain.drive_transcript import Segment, StitchedTranscript, WindowSpec


def _gold(lines, speakers=2, probes=None, family="dialogue_en", language="en", synth=""):
    return {"id": "c", "family": family, "language": language, "speakers": speakers, "synth": synth,
            "probes": probes if probes is not None else [ln[0] for ln in lines],
            "lines": [{"speaker": s, "start_s": t, "text": text, "language": language} for t, s, text in lines]}


def _st(segments, speakers=2, uncertain=False, truncated=False):
    return StitchedTranscript(segments=tuple(segments), language_codes=("en",), speakers=speakers, duration_s=100.0,
                              uncertain_seam=uncertain, truncated=truncated)


LINES = [(4.0, 0, "Good morning to you all"), (16.0, 1, "The numbers look fine this week"),
         (28.0, 0, "Then we agree on Thursday")]


def test_wer_is_zero_for_identical_text_ignoring_case_and_punctuation():
    assert m.wer("Hello, World!", "hello world") == 0.0


def test_wer_counts_substitution_deletion_and_insertion():
    assert m.wer("a b c d", "a x c d") == 0.25
    assert m.wer("a b c d", "a b c") == 0.25
    assert m.wer("a b", "a b c c") == 1.0


def test_wer_handles_cyrillic_words():
    assert m.wer("Доброго ранку", "доброго ранку") == 0.0 and m.wer("Доброго ранку", "доброго вечора") == 0.5


def test_a_perfect_transcript_scores_perfectly():
    segs = [Segment(t + 0.5, s, text) for t, s, text in LINES]
    out = m.clip_metrics(_gold(LINES), _st(segs))
    assert out["wer"] == 0.0 and out["speaker_agreement"] == 1.0 and out["speaker_overcount"] == 0
    assert out["timestamp_error_mean_s"] == pytest.approx(0.5) and out["timestamp_error_max_s"] == pytest.approx(0.5)
    assert out["lines_lost"] == 0 and out["lines_duplicated"] == 0 and out["interpolated_share"] == 0.0


def test_speaker_labels_are_arbitrary_but_must_be_consistent():
    swapped = [Segment(t, 1 - s, text) for t, s, text in LINES]           # the same split under different numbers
    assert m.clip_metrics(_gold(LINES), _st(swapped))["speaker_agreement"] == 1.0
    one_wrong = [Segment(LINES[0][0], 0, LINES[0][2]), Segment(LINES[1][0], 1, LINES[1][2]),
                 Segment(LINES[2][0], 1, LINES[2][2])]
    assert m.clip_metrics(_gold(LINES), _st(one_wrong))["speaker_agreement"] == pytest.approx(2 / 3)


def test_unlabelled_segments_have_no_agreement_value():
    segs = [Segment(t, None, text) for t, _, text in LINES]
    assert "speaker_agreement" not in m.clip_metrics(_gold(LINES), _st(segs, speakers=1))


def test_speaker_overcount_is_the_excess_only():
    segs = [Segment(t, s, text) for t, s, text in LINES]
    assert m.clip_metrics(_gold(LINES), _st(segs, speakers=4))["speaker_overcount"] == 2
    assert m.clip_metrics(_gold(LINES), _st(segs, speakers=1))["speaker_overcount"] == 0


def test_timestamp_error_is_read_at_the_probe_lines_only():
    segs = [Segment(4.0, 0, LINES[0][2]), Segment(16.0 + 10, 1, LINES[1][2]), Segment(28.0, 0, LINES[2][2])]
    out = m.clip_metrics(_gold(LINES, probes=[4.0, 28.0]), _st(segs))
    assert out["timestamp_error_max_s"] == 0.0
    out = m.clip_metrics(_gold(LINES, probes=[16.0]), _st(segs))
    assert out["timestamp_error_mean_s"] == 10.0


def test_a_lost_line_and_a_duplicated_line_are_counted():
    segs = [Segment(4.0, 0, LINES[0][2]), Segment(5.0, 0, LINES[0][2]), Segment(28.0, 0, LINES[2][2])]
    out = m.clip_metrics(_gold(LINES), _st(segs))
    assert out["lines_lost"] == 1 and out["lines_duplicated"] == 1


def test_a_line_found_inside_a_merged_segment_is_not_lost():
    merged = [Segment(4.0, 0, LINES[0][2] + " " + LINES[1][2]), Segment(28.0, 0, LINES[2][2])]
    assert m.clip_metrics(_gold(LINES), _st(merged))["lines_lost"] == 0


def test_a_gap_marker_finds_nothing():
    segs = [Segment(4.0, None, "", gap=True)]
    assert m.clip_metrics(_gold(LINES), _st(segs, truncated=True))["lines_lost"] == 3


def test_seam_counts_only_lines_near_a_window_cut():
    specs = (WindowSpec(0, 0.0, 1800.0), WindowSpec(1, 1740.0, 1800.0))   # the cut at 1770
    lines = [(10.0, 0, "Item 1 alpha beta gamma"), (1750.0, 0, "Item 2 delta epsilon zeta"),
             (1790.0, 1, "Item 3 eta theta iota")]
    segs = [Segment(10.0, 0, lines[0][2]), Segment(1750.0, 0, lines[1][2]), Segment(1751.0, 0, lines[1][2])]
    out = m.clip_metrics(_gold(lines), _st(segs), window_specs=specs)
    assert out["seam_duplicates"] == 1 and out["seam_losses"] == 1 and out["lines_lost"] == 1


def test_a_single_window_clip_reports_no_seam_numbers():
    segs = [Segment(t, s, text) for t, s, text in LINES]
    assert "seam_losses" not in m.clip_metrics(_gold(LINES), _st(segs), window_specs=(WindowSpec(0, 0.0, 100.0),))


def test_interpolated_share_and_resubmits_are_reported():
    segs = [Segment(4.0, 0, LINES[0][2], interpolated=True), Segment(16.0, 1, LINES[1][2])]
    out = m.clip_metrics(_gold(LINES), _st(segs), coverage_resubmits=2)
    assert out["interpolated_share"] == 0.5 and out["coverage_resubmits"] == 2


def test_no_speech_clip_passes_only_when_nothing_was_transcribed():
    clip = _gold([], speakers=0, family="no_speech_tone", language="", synth="tone")
    assert m.clip_metrics(clip, _st([], speakers=0))["no_speech_ok"] is True
    assert m.clip_metrics(clip, _st([Segment(0.0, None, "hello there")], speakers=1))["no_speech_ok"] is False


def _row(**kw):
    return {"id": "x", "family": "dialogue_en", "language": "en", **kw}


def test_aggregate_groups_by_family_and_keeps_ukrainian_apart():
    table = m.aggregate([_row(wer=0.1), _row(wer=0.3), {"id": "u", "family": "dialogue_uk", "language": "uk", "wer": 0.5}])
    assert table["by_family"]["dialogue_en"]["wer"] == pytest.approx(0.2)
    assert table["by_language"]["uk"]["wer"] == 0.5 and table["by_language"]["en"]["wer"] == pytest.approx(0.2)


def test_aggregate_takes_the_worst_of_the_must_be_zero_counts():
    table = m.aggregate([_row(seam_duplicates=0, seam_losses=1), _row(seam_duplicates=3, seam_losses=0)])
    assert table["by_family"]["dialogue_en"]["seam_duplicates"] == 3 and table["by_family"]["dialogue_en"]["seam_losses"] == 1


def test_bars_round_in_the_safe_direction_and_regressions_name_the_metric():
    table = m.aggregate([_row(wer=0.123456789, speaker_agreement=0.987654321)])
    bars = m.make_bars(table, recorded_at="t", gold_sha256="g", model="x")
    row = bars["by_family"]["dialogue_en"]
    assert row["wer"] == 0.123457 and row["speaker_agreement"] == 0.987654
    assert m.regressions(bars, table) == []
    worse = m.aggregate([_row(wer=0.2, speaker_agreement=0.9)])
    problems = m.regressions(bars, worse)
    assert any("wer" in p for p in problems) and any("speaker_agreement" in p for p in problems)


def test_a_family_missing_from_the_run_is_a_named_problem():
    bars = m.make_bars(m.aggregate([_row(wer=0.1)]), recorded_at="t", gold_sha256="g", model="x")
    assert any("not measured" in p for p in m.regressions(bars, m.aggregate([])))


def test_no_speech_bar_falls_when_a_run_transcribes_a_tone():
    clip = {"id": "t", "family": "no_speech_tone", "language": "", "no_speech_ok": True}
    bars = m.make_bars(m.aggregate([clip]), recorded_at="t", gold_sha256="g", model="x")
    assert m.regressions(bars, m.aggregate([{**clip, "no_speech_ok": False}]))
