"""drive_sync dedup keys and payloads (USER_DRIVE_INDEXING_RFC §7.1; plan Δ2-Δ4)."""
from datetime import datetime, timedelta, timezone

from src.domain.drive_index import Nudge
from src.domain.drive_task_keys import (
    DRIVE_SYNC_TASK, continuation_key, nudge_key, parse_nudge, tick_key, tick_payload, toggle_key,
)

NOW = datetime(2026, 10, 11, 12, 3, tzinfo=timezone.utc)


def test_tick_key_is_stable_within_a_bucket_and_changes_across():
    assert tick_key("u", NOW) == tick_key("u", NOW + timedelta(minutes=6))
    assert tick_key("u", NOW) != tick_key("u", NOW + timedelta(minutes=10))
    assert tick_key("u", NOW).startswith("u:tick:")


def test_family_keys():
    assert DRIVE_SYNC_TASK == "drive_sync"
    assert continuation_key("u", "t1") == "u:cont:t1"
    assert nudge_key("u", "m1", 0) == "u:nudge:m1" and nudge_key("u", "m1", 2) == "u:nudge:m1:r2"
    assert toggle_key("u", NOW) != toggle_key("u", NOW + timedelta(milliseconds=1))


def test_payload_round_trip():
    nudge = Nudge(mutation_id="m1", item_ids=("a", "b"), attempt=1)
    assert tick_payload("u") == {"user_id": "u"}
    assert parse_nudge(tick_payload("u", nudge)) == nudge
    assert parse_nudge({"user_id": "u"}) is None and parse_nudge({"nudge": {"item_ids": ["a"]}}) is None
