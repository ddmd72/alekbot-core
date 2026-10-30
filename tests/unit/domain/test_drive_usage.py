"""The usage and initial-build arithmetic both repository adapters apply (USER_DRIVE_INDEXING_RFC §25.4.5, D27), and the
lean projection of a record (§25.13.3)."""
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_enrichment import InitialBuild, ModelUsageMonth
from src.domain.drive_index import DriveIndexRecord, RecordState
from src.domain.drive_index_lean import RECORD_LEAN_FIELDS, DriveRecordLean, lean_of
from src.domain.drive_index_params import INITIAL_BUILD_USD
from src.domain.drive_usage import (
    MODEL_USAGE_TTL, added_usage, closed_initial, initial_build_doc_id, new_initial_build, spent_initial, usage_doc_id,
    usage_expires_at,
)

T = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def test_document_ids_and_the_ttl():
    assert usage_doc_id("u", "2026-10") == "u:2026-10"
    assert initial_build_doc_id("u", "realm_a", "sub-1") == "u:initial:realm_a:sub-1"
    assert MODEL_USAGE_TTL == timedelta(days=400)
    assert usage_expires_at("2026-10") == datetime(2026, 10, 1, tzinfo=timezone.utc) + MODEL_USAGE_TTL


def test_added_usage_per_class_fallback_calls_and_day():
    one = added_usage(ModelUsageMonth(), "2026-10-13", "triage", 0.5, 0.25, 2)
    two = added_usage(one, "2026-10-13", "reader", 1.0, 0.0, 1)
    assert two == ModelUsageMonth(reader_usd=1.0, triage_usd=0.5, fallback_usd=0.25, calls=3,
                                  daily={"2026-10-13": (1.5, 0.25)})
    assert one.daily == {"2026-10-13": (0.5, 0.25)}   # the input is not changed
    with pytest.raises(ValueError):
        added_usage(ModelUsageMonth(), "2026-10-13", "pages", 1.0, 0.0, 1)


class TestInitialBuild:
    def test_new_is_unopened_with_the_allowance(self):
        assert new_initial_build() == InitialBuild(allowance_usd=INITIAL_BUILD_USD)

    def test_a_spend_opens_and_covers(self):
        assert spent_initial(InitialBuild(10.0), 4.0, T) == (InitialBuild(10.0, 4.0, T, None), 4.0)

    def test_the_last_dollars_close_it(self):
        build, covered = spent_initial(InitialBuild(10.0, 8.0, T, None), 5.0, T + timedelta(hours=1))
        assert (build, covered) == (InitialBuild(10.0, 10.0, T, T + timedelta(hours=1)), 2.0)

    def test_float_dust_closes_it(self):
        build, _ = spent_initial(InitialBuild(0.3, 0.1, T, None), 0.2, T)
        assert build.closed_at == T

    def test_a_closed_build_covers_nothing(self):
        closed = InitialBuild(10.0, 1.0, T, T)
        assert spent_initial(closed, 5.0, T + timedelta(days=1)) == (closed, 0.0)

    def test_no_dollars_change_nothing(self):
        assert spent_initial(InitialBuild(10.0), 0.0, T) == (InitialBuild(10.0), 0.0)

    def test_close_keeps_its_first_time(self):
        once = closed_initial(InitialBuild(10.0), T)
        assert once.closed_at == T and closed_initial(once, T + timedelta(days=1)) == once


def test_lean_of_takes_exactly_the_lean_fields():
    assert RECORD_LEAN_FIELDS == tuple(f.name for f in fields(DriveRecordLean))
    assert set(RECORD_LEAN_FIELDS) <= {f.name for f in fields(DriveIndexRecord)}
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", parent_id="p", name="n", kind="pdf", effective_date=T,
                           about_keys=("e:x",), name_tokens=("w:n",), state=RecordState.INDEXED, has_card=True,
                           indexed_generation=3, content_identity=ContentIdentity("fam1", "h", 1), valid_until=T, readme_excerpt="not lean")
    lean = lean_of(rec)
    assert lean.item_id == "i" and lean.about_keys == ("e:x",) and lean.valid_until == T
    assert lean.indexed_generation == 3 and lean.has_card
