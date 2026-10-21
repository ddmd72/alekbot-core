"""Pure selectors of the fact S1 backfill script."""
import asyncio
from datetime import datetime, timezone

from google.api_core.exceptions import FailedPrecondition

from scripts.migration import fact_s1_backfill as bf

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_superseded_is_current_true_and_no_valid_to():
    doc = {"state": "superseded", "is_current": True, "valid_to": None, "last_updated": T2, "created_at": T1}
    assert bf.select_flags_update(doc) == {"is_current": False, "valid_to": T2}


def test_superseded_with_none_valid_to_only():
    doc = {"state": "superseded", "is_current": False, "valid_to": None, "last_updated": T2}
    assert bf.select_flags_update(doc) == {"valid_to": T2}


def test_superseded_already_correct():
    doc = {"state": "superseded", "is_current": False, "valid_to": T1, "last_updated": T2}
    assert bf.select_flags_update(doc) is None


def test_archived_without_valid_to_uses_last_updated():
    doc = {"state": "archived", "is_current": True, "valid_to": None, "last_updated": T2, "created_at": T1}
    assert bf.select_flags_update(doc) == {"valid_to": T2}


def test_invalidated_falls_back_to_created_at():
    doc = {"state": "invalidated", "is_current": True, "valid_to": None, "created_at": T1}
    assert bf.select_flags_update(doc) == {"valid_to": T1}


def test_current_and_stateless_untouched():
    assert bf.select_flags_update({"state": "current", "is_current": True, "valid_to": None, "last_updated": T1}) is None
    assert bf.select_flags_update({"is_current": True, "last_updated": T1}) is None


def test_date_string_with_z():
    assert bf.select_date_update({"reported_date": "2026-03-04T10:00:00Z"}) == {
        "reported_date": datetime(2026, 3, 4, 10, tzinfo=timezone.utc)}


def test_date_naive_string_becomes_utc():
    assert bf.select_date_update({"reported_date": "2026-03-04T10:00:00"}) == {
        "reported_date": datetime(2026, 3, 4, 10, tzinfo=timezone.utc)}


def test_date_unparseable():
    doc = {"reported_date": "not a date"}
    assert bf.select_date_update(doc) is None
    assert bf.is_unparseable_date(doc)


def test_date_timestamp_untouched():
    doc = {"reported_date": T1}
    assert bf.select_date_update(doc) is None
    assert not bf.is_unparseable_date(doc)


def test_version_one_not_candidate():
    assert bf.select_vector_candidate({"version": 1, "text": "x", "state": "current"}) is None


def test_version_three_archived_is_candidate():
    texts = bf.select_vector_candidate({"version": 3, "text": "x", "state": "archived", "tags": ["a"], "metadata": {"k": 1}})
    assert texts is not None and texts[0] == "x" and texts[1].endswith("a")


def test_audit_archived_not_current():
    assert bf.audit_classes({"state": "archived", "is_current": False}) == ["archived/invalidated with is_current False"]
    assert bf.audit_classes({"state": "invalidated", "is_current": False})
    assert bf.audit_classes({"state": "archived", "is_current": True}) == []


def test_audit_current_stale_flags():
    assert bf.audit_classes({"state": "current", "is_current": False, "valid_to": T1}) == [
        "current/stale with is_current False", "current/stale with valid_to set"]
    assert bf.audit_classes({"state": "stale", "is_current": True, "valid_to": T1}) == [
        "current/stale with valid_to set"]
    assert bf.audit_classes({"state": "current", "is_current": True, "valid_to": None}) == []
    assert bf.audit_classes({"state": "superseded", "is_current": True}) == []


class _Ref:
    id = "abcdefgh1234"

    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []

    async def update(self, payload, option=None):
        self.calls.append((payload, option))
        if self.exc:
            raise self.exc


def _opt(**kw):
    return ("opt", kw)


def test_guarded_update_passes_precondition():
    ref = _Ref()
    assert asyncio.run(bf.guarded_update(ref, {"a": 1}, T1, _opt)) == bf.WRITE_OK
    assert ref.calls == [({"a": 1}, ("opt", {"last_update_time": T1}))]


def test_guarded_update_counts_precondition_failure_separately():
    assert asyncio.run(bf.guarded_update(_Ref(FailedPrecondition("x")), {}, T1, _opt)) == bf.WRITE_CHANGED


def test_guarded_update_other_error_is_failed():
    assert asyncio.run(bf.guarded_update(_Ref(RuntimeError("x")), {}, T1, _opt)) == bf.WRITE_FAILED
