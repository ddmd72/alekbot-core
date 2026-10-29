"""FirestoreDriveIndexRepository codec: every timestamp Firestore hands back (DatetimeWithNanoseconds) is read as a
plain tz-aware UTC `datetime`, including the one nested in a document brief (USER_DRIVE_INDEXING_RFC §3.2, §3.5,
§24.4). Equality cannot prove this — DatetimeWithNanoseconds compares equal to datetime — so the type is asserted."""
import collections.abc
import typing
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import (
    RECORD_FANOUT_FIELDS, record_from_doc, record_to_doc, state_from_doc, state_to_doc,
)
from src.domain.drive_index import DocumentBrief, DriveIndexRecord, DriveSyncState

T = datetime(2026, 10, 12, 9, 15, 30, 250000, tzinfo=timezone.utc)
# The state's timestamp fan-out flags: written only by update_sync_flags, never by save_sync_state (R1's mark)
_STATE_FANOUT_TIMESTAMPS = frozenset({"disconnected_by_user_at"})


def _firestore_ts(value: datetime) -> DatetimeWithNanoseconds:
    return DatetimeWithNanoseconds(value.year, value.month, value.day, value.hour, value.minute, value.second,
                                   value.microsecond, tzinfo=timezone.utc)


def _stored(value):
    """What Firestore hands back: timestamps as DatetimeWithNanoseconds, everything else as written."""
    if isinstance(value, datetime):
        return _firestore_ts(value)
    if isinstance(value, dict):
        return {k: _stored(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stored(v) for v in value]
    return value


def _is_map(hint) -> bool:
    return typing.get_origin(hint) in (dict, collections.abc.Mapping)


def _datetime_fields(cls) -> typing.List[str]:
    """Every field typed datetime / Optional[datetime], found by type so a new timestamp field is covered."""
    hints = typing.get_type_hints(cls)
    return [f.name for f in fields(cls)
            if hints[f.name] is datetime or (datetime in typing.get_args(hints[f.name]) and not _is_map(hints[f.name]))]


def _datetime_map_fields(cls) -> typing.List[str]:
    """Every field typed Mapping[str, datetime] (§25.4.6 `model_backoff_by_agent`), found by type likewise."""
    hints = typing.get_type_hints(cls)
    return [f.name for f in fields(cls) if _is_map(hints[f.name]) and typing.get_args(hints[f.name])[1:] == (datetime,)]


def _assert_plain_utc(value, name):
    assert type(value) is datetime, f"{name}: {type(value).__name__}"
    assert value.tzinfo is not None and value.utcoffset() == timedelta(0), name


def test_both_models_have_timestamp_fields():
    assert "defer_until" in _datetime_fields(DriveIndexRecord)
    assert "model_backoff_until" in _datetime_fields(DriveSyncState)
    assert _datetime_map_fields(DriveSyncState) == ["model_backoff_by_agent"]
    assert not _datetime_map_fields(DriveIndexRecord)


@pytest.mark.parametrize("name", _datetime_fields(DriveIndexRecord))
def test_record_timestamp_reads_back_as_plain_utc_datetime(name):
    rec = DriveIndexRecord(user_id="u", item_id="i", **{name: T})
    written = record_to_doc(rec)
    if name in RECORD_FANOUT_FIELDS:
        # the fan-out's own field (USER_DRIVE_INDEXING_RFC §26.2, Δ54): never part of the tick's record writes;
        # set_inaccessible_notified writes the plain datetime with a field update, record_from_doc reads it
        assert name not in written
        written = {**written, name: T}
    doc = _stored(written)
    assert type(doc[name]) is DatetimeWithNanoseconds   # the input really is Firestore-shaped
    back = getattr(record_from_doc(doc), name)
    _assert_plain_utc(back, name)
    assert back == T


@pytest.mark.parametrize("name", _datetime_fields(DriveSyncState))
def test_state_timestamp_reads_back_as_plain_utc_datetime(name):
    state = DriveSyncState(user_id="u", **{name: T})
    written = state_to_doc(state)
    if name in _STATE_FANOUT_TIMESTAMPS:
        # a fan-out flag (SECRETS_AT_REST_RFC §6.2 F1): never part of the tick's state writes; update_sync_flags
        # writes the plain datetime with a field update, state_from_doc reads it
        assert name not in written
        written = {**written, name: T}
    doc = _stored(written)
    assert type(doc[name]) is DatetimeWithNanoseconds
    back = getattr(state_from_doc("u", doc), name)
    _assert_plain_utc(back, name)
    assert back == T


@pytest.mark.parametrize("name", _datetime_map_fields(DriveSyncState))
def test_state_timestamp_map_reads_back_as_plain_utc_datetimes(name):
    """A map of timestamps: every value Firestore hands back is read as a plain UTC datetime, keys kept."""
    written = {"drive_reader": T, "drive_profiler": T + timedelta(minutes=5)}
    state = DriveSyncState(user_id="u", **{name: written})
    doc = _stored(state_to_doc(state))
    assert {type(v) for v in doc[name].values()} == {DatetimeWithNanoseconds}
    back = getattr(state_from_doc("u", doc), name)
    assert set(back) == set(written)
    for key, value in back.items():
        _assert_plain_utc(value, f"{name}[{key}]")
    assert dict(back) == written


def test_brief_doc_date_reads_back_as_plain_utc_datetime():
    rec = DriveIndexRecord(user_id="u", item_id="i",
                           brief=DocumentBrief(doc_type="invoice", doc_date=T, parties=("A", "B")))
    doc = _stored(record_to_doc(rec))
    assert type(doc["brief"]["doc_date"]) is DatetimeWithNanoseconds
    back = record_from_doc(doc).brief
    _assert_plain_utc(back.doc_date, "brief.doc_date")
    assert back.doc_date == T and back.parties == ("A", "B") and type(back.parties) is tuple
