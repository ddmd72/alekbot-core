"""The indexed body, the card segment and the opened text (RFC §27.8 a, §27.11; G17-G19). Synthetic text only."""
import pytest

from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_text_gate import is_machine_text
from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript
from src.domain.drive_transcript_render import (
    TRANSCRIPT_BODY_VERSION, card_segment, open_text, render_body,
)


def _seg(start, text, speaker=None, **kw):
    return Segment(start_s=float(start), speaker=speaker, text=text, **kw)


def _st(segments, speakers=2, **kw):
    return StitchedTranscript(segments=tuple(segments), language_codes=("uk", "en"), speakers=speakers,
                              duration_s=2820.0, **kw)


def test_body_version_pinned():
    assert TRANSCRIPT_BODY_VERSION == 1


def test_body_format_and_stamp_at_paragraph_heads():
    body = render_body(_st([_seg(5, "hello there.", 1), _seg(3725, "second line", 2, interpolated=True),
                            _seg(3790, "third\nline  here", 1)]))
    assert body == ("[00:05] Speaker 1: hello there.\n\n"
                    "[~1:02:05] Speaker 2: second line\n\n"
                    "[1:03:10] Speaker 1: third line here")
    for paragraph in body.split("\n\n"):
        assert paragraph.startswith("[")


def test_single_speaker_has_no_labels():
    body = render_body(_st([_seg(0, "one.", None), _seg(70, "two.", None)], speakers=1))
    assert body == "[00:00] one.\n\n[01:10] two."
    assert "Speaker" not in body


def test_gap_marker_render():
    body = render_body(_st([_seg(0, "before.", 1), _seg(600, "ignored text", None, gap=True), _seg(1200, "after.", 1)],
                           truncated=True))
    assert "[10:00] [this part could not be transcribed]" in body
    assert "ignored text" not in body


def test_render_is_language_neutral():
    body = render_body(_st([_seg(0, "Привіт, світе.", 1), _seg(9, "Hola, mundo.", 2)]))
    assert body == "[00:00] Speaker 1: Привіт, світе.\n\n[00:09] Speaker 2: Hola, mundo."


def _rec(**kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="audio", **kw)


@pytest.mark.parametrize("kw,expected", [
    (dict(transcript_state="current", transcript_seconds=2820, transcript_speakers=2,
          transcript_languages=("uk", "en")), "transcript: 47 min, 2 speakers, uk/en"),
    (dict(transcript_state="current", transcript_seconds=60, transcript_speakers=1,
          transcript_languages=("en",)), "transcript: 1 min, 1 speaker, en"),
    (dict(transcript_state="current", transcript_seconds=600), "transcript: 10 min"),
    (dict(transcript_state="current", transcript_stub="no_speech"), "transcript: none (no speech detected)"),
    (dict(transcript_state="current", transcript_stub="too_long", transcript_seconds=18720),
     "transcript: none (too long: 5:12:00)"),
    (dict(transcript_state="current", transcript_stub="too_large"), "transcript: none (too large)"),
    (dict(transcript_state="current", transcript_stub="no_audio"), "transcript: none (no audio track)"),
    (dict(), None),
    (dict(transcript_state="working"), None),
    (dict(transcript_state="failed", transcript_stub="failed"), None),
])
def test_card_segment_for_each_stub(kw, expected):
    assert card_segment(_rec(**kw)) == expected


def _stored(transcript=None, stub=""):
    return StoredTranscript(content_key="k", transcript=transcript, stub=stub)


def test_open_text_cut_with_note():
    segs = [_seg(i * 10, f"line number {i} with some padding words", 1 + i % 2) for i in range(200)]
    transcript = _st(segs)
    full = render_body(transcript)
    assert open_text(_stored(transcript), max_chars=len(full) + 10) == full       # under the cap: untouched
    cut = open_text(_stored(transcript), max_chars=1000)
    body, _, note = cut.partition("\n\n[Transcript cut")
    assert body == render_body(_st(segs[:len(body.split("\n\n"))]))                # whole paragraphs only
    assert len(cut) <= 1000
    assert note.endswith("Search for a passage to read a specific part.]")
    assert f"of {len(full)} characters" in note
    assert "folder" not in note and "mirror" not in note


def test_open_text_note_points_at_mirror_path_when_given():
    segs = [_seg(i * 10, f"line number {i} with some padding words", 1) for i in range(200)]
    cut = open_text(_stored(_st(segs)), max_chars=1000, mirror_path="Transcripts/a.mp3.md")
    assert "The full text is in Transcripts/a.mp3.md." in cut
    assert len(cut) <= 1000


def test_open_text_cap_includes_seam_line():
    segs = [_seg(i * 10, f"line number {i} with some padding words", 1) for i in range(200)]
    cut = open_text(_stored(_st(segs, uncertain_seam=True)), max_chars=1000)
    assert cut.startswith("[speaker labels may differ") and len(cut) <= 1000


def test_open_text_uncertain_seam_line_and_stub():
    out = open_text(_stored(_st([_seg(0, "hi.", 1)], uncertain_seam=True)), max_chars=1000)
    assert out.startswith("[speaker labels may differ between parts of this recording]\n\n[00:00]")
    assert open_text(_stored(None, stub="no_speech"), max_chars=1000) == "transcript: none (no speech detected)"
    assert open_text(_stored(None), max_chars=1000) == ""


def _vocabulary():
    syllables = ["ba", "ko", "ri", "mu", "te", "na", "so", "li", "pe", "du", "fa", "go"]
    return [a + b + c for a in syllables for b in syllables[:7] for c in ("n", "r", "l")]     # 252 invented words


def _two_speaker_40_min():
    vocab, state, segs = _vocabulary(), 12345, []
    for i in range(160):                                   # a line every 15 s over 40 minutes
        words = []
        for _ in range(14):
            state = (state * 1103515245 + 12345) % 2 ** 31
            words.append(vocab[state % len(vocab)])
        segs.append(_seg(i * 15, " ".join(words).capitalize() + ".", 1 + i % 2))
    return _st(segs)


def test_rendered_transcripts_pass_text_gate():
    two = render_body(_two_speaker_40_min())
    assert not is_machine_text(two)
    one_t = _two_speaker_40_min()
    one = render_body(StitchedTranscript(
        segments=tuple(Segment(s.start_s, None, s.text) for s in one_t.segments),
        language_codes=("en",), speakers=1, duration_s=2400.0))
    assert not is_machine_text(one)
    loop = render_body(_st([_seg(i * 4, "Thank you for watching this video today.", 1) for i in range(300)], speakers=1))
    assert is_machine_text(loop)
