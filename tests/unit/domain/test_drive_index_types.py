from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from src.domain.drive_index import (
    CURRENT_VERSIONS, DriveChange, DriveIndexRecord, DriveSyncState,
    EpochPhase, Priority, record_key,
)
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain import drive_index_params as P

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def test_change_is_frozen_and_defaults_live():
    ch = DriveChange(item_id="a")
    assert ch.deleted is False and ch.media == DriveMediaFacts()
    with pytest.raises(FrozenInstanceError):
        ch.name = "x"  # type: ignore[misc]


def test_record_defaults_have_no_work():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="a")
    assert rec.due_at is None and rec.priority is None
    assert rec.indexed_generation is None and rec.index_generation == 0
    assert rec.has_card is False and rec.card_hash is None


def test_priority_order_matches_rfc_7_8():
    assert [p.value for p in Priority] == [0, 1, 2, 3, 4]
    assert Priority.EDIT < Priority.CASCADE < Priority.FIRST_SEEN < Priority.MAINTENANCE < Priority.RETRY


def test_sync_state_starts_idle_without_cursor():
    st = DriveSyncState(user_id="u", connection_id="u")
    assert st.epoch == 0 and st.epoch_phase is EpochPhase.IDLE and st.cursor is None
    assert st.applied_versions == {} and st.purging is False and st.systemic_failures == 0


def test_versions_are_ints_keyed_by_name():
    # Search v2 (P5, §25.4.8): keys, profile and passages joined the map behind the migration gate (D30);
    # §26.4 (Δ61): accuracy, the gate key since Δ65
    assert set(CURRENT_VERSIONS) == {"policy", "card", "geo_table", "name_keys", "extraction", "triage",
                                     "keys", "profile", "passages", "accuracy"}
    assert all(isinstance(v, int) and v >= 1 for v in CURRENT_VERSIONS.values())


def test_record_key_is_stable():
    assert record_key("u1", "ABC!s12") == "u1:ABC!s12"


def test_params_match_rfc_15():
    assert P.TICK_INTERVAL.total_seconds() == 600
    assert P.TICK_DEADLINE_S == 1800
    assert P.TICK_START_BUDGET.total_seconds() == 480
    assert P.MAINTENANCE_SHARE == 0.30
    assert (P.MAX_ATTEMPTS, P.RETRY_BASE.total_seconds(), P.RETRY_CAP.total_seconds()) == (3, 300, 21600)
    assert P.EMBED_IN_FLIGHT == 4
    assert P.MAX_CARD_RECORDS_PER_USER == 25_000
    assert (P.K, P.CARD_FETCH, P.NAME_FETCH) == (8, 50, 100)
    assert P.DENSE_FOLDER_CHILDREN == 500
    assert (P.PROBE_CHILDREN, P.ITEM_CALLS_PER_TICK, P.RELIST_PAGE) == (200, 20, 200)
    assert (P.FOLDER_INMEM_CARDS, P.CASCADE_OPS_PER_TICK, P.NAME_MIN_TRIGRAMS) == (1000, 2000, 3)
    assert P.STALE_ALERT_AFTER.total_seconds() == 7200
    assert P.ACCESS_LOSS_TTL.days == 30
