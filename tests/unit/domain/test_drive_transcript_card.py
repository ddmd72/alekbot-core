"""Transcribed media in the card, the profile table and the effective date (USER_DRIVE_INDEXING_RFC §27.8 a; plan
rulings G5, G38). Synthetic records only."""
import random
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.domain import drive_profile as dp
from src.domain.drive_card import card_hash, card_text
from src.domain.drive_enrich_plan import effective_from, profiled
from src.domain.drive_enrichment import DriveProfile, ProfileDate, ProfileSource
from src.domain.drive_index import (
    CARD_VERSION, PROFILE_VERSION, DriveChange, DocumentBrief, DriveIndexRecord, Reason, RecordState,
)
from src.domain.drive_index_planner import _effective
from src.domain.drive_transcript_render import card_segment

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
FILE_DATE = datetime(2026, 3, 2, 9, tzinfo=timezone.utc)
PROFILE = DriveProfile(title="Call", what_it_is="A phone call", language="en", doc_type="note", topics=("plans",))


def media(**kw):
    base = dict(user_id="u", item_id="m", name="call.m4a", kind="audio", state=RecordState.INDEXED,
                transcript_state="current", transcript_seconds=125, transcript_speakers=2,
                transcript_languages=("en",), fs_modified_at=FILE_DATE, effective_date=FILE_DATE)
    base.update(kw)
    return DriveIndexRecord(**base)


def test_card_segment_appended_for_transcribed_media():
    seg = card_segment(media())
    assert seg and seg.startswith("transcript:")
    assert card_text(media(profile=PROFILE)).endswith(seg)               # profiled
    assert card_text(media()).endswith(seg)                              # brief branch (before the profile)
    machine = card_text(media(reason=Reason.MACHINE_TEXT, state=RecordState.METADATA_ONLY, lead_text="la la"))
    assert machine.endswith(seg) and "machine-generated text" in machine  # demoted by the gate
    stub = media(transcript_stub="no_speech", transcript_seconds=0)
    assert card_text(stub).endswith(card_segment(stub))


def test_media_without_a_transcript_has_no_segment_and_a_stable_hash():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="1", name="song.mp3", kind="audio", state=RecordState.METADATA_ONLY)
    assert "transcript" not in card_text(rec)
    assert card_hash(rec) == "b2a32339c47d5683a7880b89189b0de3"          # pinned: equal to the pre-§27 card hash
    for state in ("none", "working", "waiting", "failed"):
        assert card_text(replace(rec, transcript_state=state)) == card_text(rec)


def test_untranscribed_media_card_unchanged_hash():
    rec = media(transcript_state="none", transcript_seconds=0, transcript_speakers=0, transcript_languages=())
    assert card_hash(rec) == card_hash(DriveIndexRecord(user_id="u", connection_id="u", item_id="m", name="call.m4a", kind="audio",
                                                       state=RecordState.INDEXED))


def test_document_card_unchanged_hash():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="2", name="lease.pdf", kind="pdf", state=RecordState.INDEXED,
                           profile=DriveProfile(title="Lease", what_it_is="A lease", language="en",
                                                doc_type="contract", topics=("rent",)))
    assert card_hash(rec) == "245379c6567240f9d19e95a61d793ae1"
    # transcript fields on a non-media kind never reach the card
    assert card_text(replace(rec, transcript_state="current", transcript_seconds=60)) == card_text(rec)


def test_no_version_bump():
    assert CARD_VERSION == 4   # G5: the segment exists only on transcribed records
    assert PROFILE_VERSION == 2   # §27.13: the new rows need no profile re-run


@pytest.mark.parametrize("seed", range(5))
def test_random_records_without_transcripts_keep_their_card(seed):
    rng = random.Random(seed)
    for i in range(200):
        rec = DriveIndexRecord(
            user_id="u", item_id=str(i), name=f"f{i}", kind=rng.choice(["pdf", "audio", "video", "text", "image"]),
            state=rng.choice(list(RecordState)), reason=rng.choice([None, Reason.MACHINE_TEXT, Reason.NO_TEXT]),
            profile=PROFILE if rng.random() < .5 else None, lead_text="lead",
            transcript_state=rng.choice(["none", "working", "waiting", "failed"]))
        assert "transcript:" not in card_text(rec)
        assert card_text(rec) == card_text(replace(rec, transcript_state="none"))


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_profile_rows_for_media(kind):
    assert dp.PROFILE_POLICY[kind] == dp.KindPolicy(dp.InputRule.CUES, dp.ProfileOutput.FULL, True)
    assert dp.policy_of(kind, text_chars=10_000) == dp.PROFILE_POLICY["subtitles"]
    assert dp.record_policy(media(kind=kind), text_chars=10_000) == dp.PROFILE_POLICY["subtitles"]


class TestSpokenDatesNeverRedate:
    SPOKEN = DriveProfile(title="Call", what_it_is="A call", language="en", doc_type="note",
                          dates=(ProfileDate("issued", date(2019, 5, 4), "4 May 2019"),))

    def test_spoken_date_never_redates_a_recording(self):
        rec = media(profile=self.SPOKEN)
        assert effective_from(rec, self.SPOKEN, now=NOW) == FILE_DATE
        applied = profiled(rec, self.SPOKEN, source=ProfileSource.MODEL, fingerprint="fp", now=NOW)
        assert applied.effective_date == FILE_DATE

    def test_a_brief_date_never_redates_a_recording(self):
        rec = media(brief=DocumentBrief(doc_date=datetime(2019, 5, 4, tzinfo=timezone.utc)))
        assert effective_from(rec, None, now=NOW) == FILE_DATE

    def test_a_document_still_moves_to_its_issue_date(self):
        doc = media(kind="pdf", name="a.pdf")
        assert effective_from(doc, self.SPOKEN, now=NOW) == datetime(2019, 5, 4, tzinfo=timezone.utc)

    def test_the_planner_exempts_the_same_kinds(self):
        change = DriveChange(item_id="m", fs_modified_at=FILE_DATE, modified_at=FILE_DATE)
        brief = DocumentBrief(doc_date=datetime(2019, 5, 4, tzinfo=timezone.utc))
        for kind in ("audio", "video"):
            assert _effective(change, brief, NOW, self.SPOKEN, kind) == FILE_DATE
        assert _effective(change, brief, NOW, self.SPOKEN, "pdf") == datetime(2019, 5, 4, tzinfo=timezone.utc)
        assert _effective(change, brief, NOW, self.SPOKEN) == datetime(2019, 5, 4, tzinfo=timezone.utc)
