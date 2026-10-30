"""The stored-record shapes of slice 1b (MULTI_DRIVE_RFC §7.2, §8, §10, I7, I8): the new fields, their neutral defaults
(a document written before them reads unchanged), and the map codecs of the two nested value types."""
from dataclasses import MISSING, fields
from datetime import datetime, timezone

import pytest

from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_enrichment import DriveHintDoc
from src.domain.drive_index import DriveChunk, DriveIndexRecord, DriveSyncState
from src.domain.drive_index_lean import RECORD_LEAN_FIELDS, DriveRecordLean, lean_of
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_mirror import DeletionNotice, MirrorDoc, MirrorRoot, ledger_key, mirror_root_doc_id
from src.domain.drive_transcript import (
    RETAINED_DISCONNECT, StoredTranscript, TranscriptBatch, TranscriptJob,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
CI = ContentIdentity(family="fam1", digest="aGFzaA/+=", size=2048)
FACTS = DriveMediaFacts(taken_at=NOW, camera_make="make-1", camera_model="model-1", latitude=1.5, longitude=2.5,
                        image_width=4, image_height=3, video_duration_ms=61_000, video_width=640, video_height=480,
                        audio_duration_ms=5_000, package=True, malware=True, alias_of="fake_a:drive-1:item-9")

# entity -> {new field: neutral default}
NEW_FIELDS = {
    DriveIndexRecord: {"connection_id": "", "identity": "", "content_identity": None, "media": DriveMediaFacts()},
    DriveChunk: {"connection_id": ""},
    DriveHintDoc: {"connection_id": ""},
    DriveSyncState: {"connection_id": "", "drive_identity": ""},
    DriveRecordLean: {"content_identity": None},
    MirrorDoc: {"source_identity": ""},
    DeletionNotice: {"source_identity": ""},
    StoredTranscript: {"retained_at": None, "retained_reason": ""},
    TranscriptJob: {"connection_id": ""},
    TranscriptBatch: {"connection_id": ""},
}


@pytest.mark.parametrize("entity", list(NEW_FIELDS), ids=lambda e: e.__name__)
def test_new_fields_exist_with_neutral_defaults(entity):
    declared = {f.name: f for f in fields(entity)}
    for name, default in NEW_FIELDS[entity].items():
        assert name in declared, name
        field = declared[name]
        got = field.default_factory() if callable(field.default_factory) else field.default
        assert got == default, name


# owner scope change (no re-attach gate, I7 gone): the sync state has no `transcripts_reattached_at`; the mirror shapes
# are keyed by `drive_identity`, which replaces `drive_id` (MULTI_DRIVE_RFC §10) and has no default
REMOVED_FIELDS = {
    DriveSyncState: ("transcripts_reattached_at", "drive_id"),
    MirrorRoot: ("drive_id",),
    MirrorDoc: ("drive_id",),
}


@pytest.mark.parametrize("entity", list(REMOVED_FIELDS), ids=lambda e: e.__name__)
def test_removed_fields_are_gone(entity):
    declared = {f.name for f in fields(entity)}
    for name in REMOVED_FIELDS[entity]:
        assert name not in declared, name


@pytest.mark.parametrize("entity", [MirrorRoot, MirrorDoc], ids=lambda e: e.__name__)
def test_the_mirror_drive_identity_is_required(entity):
    field = {f.name: f for f in fields(entity)}["drive_identity"]
    assert field.default is MISSING and field.default_factory is MISSING


def test_a_record_carries_the_neutral_shapes():
    rec = DriveIndexRecord(user_id="usr-1", item_id="item-1", connection_id="conn-1",
                           identity="fake_a:drive-1:item-1", content_identity=CI, media=FACTS)
    assert (rec.connection_id, rec.identity, rec.content_identity, rec.media) == (
        "conn-1", "fake_a:drive-1:item-1", CI, FACTS)


def test_the_lean_projection_carries_the_content_identity():
    rec = DriveIndexRecord(user_id="usr-1", item_id="item-1", content_identity=CI)
    assert "content_identity" in RECORD_LEAN_FIELDS
    assert lean_of(rec).content_identity == CI


def test_retention_reason():
    head = StoredTranscript(content_key=CI.render(), transcript=None, retained_at=NOW,
                            retained_reason=RETAINED_DISCONNECT)
    assert (head.retained_at, head.retained_reason) == (NOW, "disconnect")


def test_mirror_shapes_carry_the_identities_their_keys_are_built_from():
    root = MirrorRoot(user_id="usr-1", root_item_id="r", root_name="_Transcripts",
                      drive_identity="fake_a:drive-1")
    doc = MirrorDoc(user_id="usr-1", kind="file", source_item_id="item-1",
                    drive_identity="fake_a:drive-1", source_identity="fake_a:drive-1:item-1")
    assert mirror_root_doc_id(root.user_id, root.drive_identity) == "usr-1:fake_a:drive-1"
    assert ledger_key(doc.user_id, doc.kind, doc.drive_identity, doc.source_identity).startswith("usr-1:file:")
    notice = DeletionNotice("item-1", "gone", NOW, source_identity="fake_a:drive-1:item-1")
    assert notice.source_identity == doc.source_identity


class TestContentIdentityCodec:
    def test_round_trip(self):
        assert CI.to_dict() == {"family": "fam1", "digest": "aGFzaA/+=", "size": 2048}
        assert ContentIdentity.from_dict(CI.to_dict()) == CI
        assert ContentIdentity.from_stored(CI.to_dict()) == CI

    @pytest.mark.parametrize("value", [None, {}])
    def test_nothing_stored_reads_none(self, value):
        assert ContentIdentity.from_stored(value) is None

    @pytest.mark.parametrize("value", [{"family": "fam1", "digest": "d"}, {"family": "", "digest": "d", "size": 1}])
    def test_a_malformed_map_is_refused(self, value):
        with pytest.raises((KeyError, ValueError)):
            ContentIdentity.from_dict(value)


class TestMediaFactsCodec:
    def test_round_trip_of_every_field(self):
        stored = FACTS.to_dict()
        assert set(stored) == {f.name for f in fields(DriveMediaFacts)}
        assert DriveMediaFacts.from_dict(stored) == FACTS

    @pytest.mark.parametrize("value", [None, {}])
    def test_nothing_stored_reads_the_defaults(self, value):
        assert DriveMediaFacts.from_dict(value) == DriveMediaFacts()

    def test_missing_keys_take_their_default_and_unknown_keys_are_ignored(self):
        assert DriveMediaFacts.from_dict({"image_width": 7, "later_field": 1}) == DriveMediaFacts(image_width=7)

    def test_a_stored_value_is_validated(self):
        with pytest.raises(ValueError):
            DriveMediaFacts.from_dict({"latitude": 1.0})
