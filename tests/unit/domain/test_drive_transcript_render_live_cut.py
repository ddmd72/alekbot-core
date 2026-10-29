"""A live transcript's cut carries the open bound's cut line (RFC §29.10); an archived one keeps today's note."""
from src.domain import drive_open_texts as T
from src.domain.drive_open_bound_text import BoundText
from src.domain.drive_open_cut import cut_line
from src.domain.drive_text_extent import ExtentState, ExtentWhy, TextExtent
from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript
from src.domain.drive_transcript_render import cap_plain_text, open_text, render_body

CAP = 5_000


def _stored(count=400):
    segments = tuple(Segment(start_s=float(n * 7), speaker=1 + n % 2,
                             text=f"Clause {n}. The tenant keeps the garage gate closed after 23:00.")
                     for n in range(count))
    return StoredTranscript("k", StitchedTranscript(segments, ("en",), 2, 3000.0))


def test_a_live_cut_uses_the_not_indexed_line_and_the_pointer():
    stored = _stored()
    out = open_text(stored, max_chars=CAP, mirror_path="_Transcripts/Oak Street call.md", live=True)
    assert len(out) <= CAP
    assert "Search for a passage" not in out and "[Transcript cut at" not in out
    assert T.TRANSCRIPT_SUBJECT in out
    assert T.WHY[ExtentWhy.NO_TEXT] in out
    assert "The full text is in _Transcripts/Oak Street call.md." in out
    assert render_body(stored.transcript).startswith(out.split("\n\n[Cut by")[0][:200])


def test_indexed_picks_the_third_form():
    out = open_text(_stored(), max_chars=CAP, live=True, indexed=True)
    head, _, line = out.rpartition("\n\n")
    total = len(render_body(_stored().transcript))
    expected = cut_line(T.TRANSCRIPT_SUBJECT, BoundText("", total, len(head), ExtentState.PAGEABLE),
                        TextExtent(ExtentState.PAGEABLE))
    assert line == expected
    assert len(out) <= CAP


def test_a_live_transcript_within_the_cap_is_whole():
    stored = _stored(3)
    assert open_text(stored, max_chars=CAP, live=True) == render_body(stored.transcript)


def test_archived_keeps_the_old_note():
    stored = _stored()
    archived = open_text(stored, max_chars=CAP)
    assert "[Transcript cut at" in archived and "Search for a passage" in archived
    plain = cap_plain_text(render_body(stored.transcript), max_chars=CAP)
    assert plain == archived
