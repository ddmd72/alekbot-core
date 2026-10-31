"""A transcript flagged `truncated` says so on the card and in the opened text (RFC §27.5, T-R31). Synthetic text only."""
from datetime import datetime, timezone

from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript
from src.domain.drive_transcript_plan import mark_current
from src.domain.drive_transcript_render import TRUNCATED_NOTE, UNCERTAIN_SEAM_LINE, card_segment, open_text


def _rec(**kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="i", identity="microsoft_onedrive:d1:i", kind="audio", **kw)


def _stored(**kw):
    t = StitchedTranscript(segments=(Segment(start_s=0.0, speaker=None, text="hello there"),),
                           language_codes=("en",), speakers=1, duration_s=60.0, **kw)
    return StoredTranscript(content_key="k", transcript=t)


def test_card_says_incomplete_only_when_truncated():
    base = dict(transcript_state="current", transcript_seconds=600, transcript_speakers=2, transcript_languages=("en",))
    assert card_segment(_rec(**base)) == "transcript: 10 min, 2 speakers, en"
    assert card_segment(_rec(transcript_truncated=True, **base)) == "transcript: 10 min, 2 speakers, en, may be incomplete"


def test_open_text_note_on_top_only_when_truncated():
    assert open_text(_stored(), max_chars=1000) == "[00:00] hello there"
    out = open_text(_stored(truncated=True), max_chars=1000)
    assert out == TRUNCATED_NOTE + "\n\n[00:00] hello there"


def test_open_text_note_and_seam_line_together_within_cap():
    out = open_text(_stored(truncated=True, uncertain_seam=True), max_chars=1000)
    assert out.startswith(TRUNCATED_NOTE + "\n\n" + UNCERTAIN_SEAM_LINE)


def test_mark_current_carries_truncated_onto_the_record():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    rec = mark_current(_rec(), now, seconds=600, speakers=1, languages=("en",), truncated=True)
    assert rec.transcript_truncated is True
    assert mark_current(_rec(), now, seconds=600, speakers=1, languages=("en",)).transcript_truncated is False
