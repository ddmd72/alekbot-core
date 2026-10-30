"""Drive search domain: filter-only answers, the building rule, date filters and the gate's drive id guard
(USER_DRIVE_INDEXING_RFC §10.1, §10.2)."""
from datetime import datetime, timezone

import pytest

from src.domain.drive_index import DriveIndexRecord, DriveSyncState, RecordState, Scope
from src.domain.drive_search import (
    STATUS_OK, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult, in_date_range, make_card,
    render_results, request_from_fields, search_gate,
)
from src.domain.drive_connection import DriveConnection

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def rec(item_id, **kw):
    base = dict(user_id="u", item_id=item_id, parent_id="root", name=f"{item_id}.pdf", kind="pdf",
                scope=Scope.NORMAL, state=RecordState.METADATA_ONLY, has_card=True)
    base.update(kw)
    return DriveIndexRecord(**base)


def card(item_id):
    return make_card(rec(item_id), f"{item_id}.pdf", f"{item_id} | PDF document", "a1b2c3d4e5f60718", "a1")


# --- An all-empty request has no filters to match ----------------------------------------------

class TestNoFiltersGiven:
    def test_all_empty_request_says_newest_files_not_match_the_filters(self):
        result = DriveSearchResult(status=STATUS_OK, cards=(card("f1"), card("f2")),
                                   request=request_from_fields(None, None, None, None, None),
                                   matched_total=50, matched_at_least=True)
        text = render_results(result)
        assert "match the filters" not in text
        assert "No name words or filters were given: these are the 2 newest files and folders" in text

    def test_a_kind_filter_still_says_how_many_matched(self):
        result = DriveSearchResult(status=STATUS_OK, cards=(card("f1"),), request=DriveSearchRequest(kind="pdf"),
                                   matched_total=50, matched_at_least=True)
        assert "at least 50 files and folders match the filters; showing the newest 1." in render_results(result)


# --- While building, no "0 files match" next to "the index is still being built" ---------

class TestBuildingFilterOnly:
    def test_building_filter_only_drops_the_zero_count_line(self):
        result = DriveSearchResult(status=STATUS_OK, envelope=DriveSearchEnvelope(building=True),
                                   request=DriveSearchRequest(kind="spreadsheet"), matched_total=0)
        text = render_results(result)
        # The building fact stays; "do not say a file does not exist" is Smart's protocol token now (§24.10)
        assert "The index is still being built: some files and folders may not be in it yet." in text
        assert "do not say a file does not exist" not in text
        assert "0 files" not in text and "match the filters" not in text and "newest 0" not in text

    def test_building_all_empty_request_drops_the_zero_newest_line(self):
        result = DriveSearchResult(status=STATUS_OK, envelope=DriveSearchEnvelope(building=True),
                                   request=DriveSearchRequest(), matched_total=0)
        assert "newest 0" not in render_results(result)

    def test_building_with_matches_still_says_how_many_matched(self):
        result = DriveSearchResult(status=STATUS_OK, cards=(card("f1"),),
                                   envelope=DriveSearchEnvelope(building=True),
                                   request=DriveSearchRequest(kind="spreadsheet"), matched_total=3)
        assert "3 files and folders match" in render_results(result)


# --- A capped filter-only page with no survivors is not "no matches" ----------------------

class TestFilterOnlyHonesty:
    def test_capped_page_with_no_cards_says_that_does_not_mean_none(self):
        result = DriveSearchResult(status=STATUS_OK, request=DriveSearchRequest(folder="Archive"),
                                   matched_total=0, matched_at_least=True, folder_path="Archive")
        text = render_results(result)
        assert "No matching files or folders" not in text and "match the filters" not in text
        assert "does not mean there are none" in text and "Browse the folder, or narrow the dates." in text

    def test_an_uncapped_filter_only_zero_still_says_no_matches(self):
        result = DriveSearchResult(status=STATUS_OK, request=DriveSearchRequest(kind="pdf"), matched_total=0)
        text = render_results(result)
        assert "0 files and folders match the filters" in text and "No matching files or folders." in text
        assert "does not mean there are none" not in text

    @pytest.mark.parametrize("request_", [
        DriveSearchRequest(folder="Work"),
        DriveSearchRequest(date_from=datetime(2025, 1, 1, tzinfo=timezone.utc)),
        DriveSearchRequest(date_to=datetime(2025, 12, 31, tzinfo=timezone.utc)),
    ])
    def test_each_filter_alone_still_says_how_many_matched(self, request_):
        result = DriveSearchResult(status=STATUS_OK, cards=(card("f1"),), request=request_, matched_total=5,
                                   folder_path="Work" if request_.folder else None)
        text = render_results(result)
        assert "5 files and folders match the filters; showing the newest 1." in text
        assert "No name words or filters were given" not in text


# --- Dates and the gate guard ----------------------------------------------------------------------

class TestDateFilter:
    def test_an_undated_record_never_passes_a_date_filter(self):
        request = request_from_fields("x", None, None, "2025-01-01", None)
        assert not in_date_range(rec("a", effective_date=None), request)
        assert not in_date_range(rec("a", effective_date=None), request_from_fields("x", None, None, None,
                                                                                     "2025-01-01"))

    def test_both_bounds_are_inclusive(self):
        request = request_from_fields("x", None, None, "2025-08-01", "2025-08-31")
        start = datetime(2025, 8, 1, tzinfo=timezone.utc)
        last = datetime(2025, 8, 31, 23, 59, 59, tzinfo=timezone.utc)
        assert in_date_range(rec("a", effective_date=start), request)
        assert in_date_range(rec("b", effective_date=last), request)
        assert not in_date_range(rec("c", effective_date=datetime(2025, 9, 1, tzinfo=timezone.utc)), request)
        exact = request_from_fields("x", None, None, "2025-08-01T00:00:00Z", "2025-08-31T23:59:59Z")
        assert exact.date_to == last
        assert in_date_range(rec("d", effective_date=last), exact)
        assert in_date_range(rec("e", effective_date=start), exact)

    def test_a_datetime_without_a_zone_is_utc(self):
        request = request_from_fields("x", None, None, "2025-08-01T10:00", None)
        assert request.date_from == datetime(2025, 8, 1, 10, tzinfo=timezone.utc)
        assert request.date_from.tzinfo is not None
        assert in_date_range(rec("a", effective_date=datetime(2025, 8, 1, 10, tzinfo=timezone.utc)), request)


class TestGateDriveIdGuard:
    def test_a_state_without_drive_id_is_not_another_drive(self):
        connection = DriveConnection(connection_id="conn-1", user_id="u", account_id="acc-1", credential_id="cred-1",
                                     kind="fake_a", drive_identity="fake_a:d2", ref_tag="a1", created_at=NOW)
        gate = search_gate(True, DriveSyncState("u"), connection, NOW)
        assert gate.status == STATUS_OK
