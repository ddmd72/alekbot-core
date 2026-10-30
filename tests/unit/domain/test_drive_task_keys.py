"""drive_sync dedup keys and payloads (USER_DRIVE_INDEXING_RFC §7.1; RFC Δ2-Δ4; per connection, MULTI_DRIVE_RFC §8)."""
from datetime import datetime, timedelta, timezone

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import Nudge
from src.domain.drive_task_keys import (
    DRIVE_SYNC_TASK, continuation_task_key, nudge_task_key, parse_nudge, scope_payload, tick_task_key,
    toggle_task_key,
)

NOW = datetime(2026, 10, 11, 12, 3, tzinfo=timezone.utc)
SCOPE = ConnectionScope("usr-1", "conn-1")


def test_tick_key_is_stable_within_a_bucket_and_changes_across():
    assert tick_task_key(SCOPE, NOW) == tick_task_key(SCOPE, NOW + timedelta(minutes=6))
    assert tick_task_key(SCOPE, NOW) != tick_task_key(SCOPE, NOW + timedelta(minutes=10))
    assert tick_task_key(SCOPE, NOW).startswith("conn-1:tick:")


def test_family_keys():
    assert DRIVE_SYNC_TASK == "drive_sync"
    assert continuation_task_key(SCOPE, "t1") == "conn-1:cont:t1"
    assert (nudge_task_key(SCOPE, "m1", 0) == "conn-1:nudge:m1"
            and nudge_task_key(SCOPE, "m1", 2) == "conn-1:nudge:m1:r2")
    assert toggle_task_key(SCOPE, NOW) != toggle_task_key(SCOPE, NOW + timedelta(milliseconds=1))


def test_payload_round_trip():
    nudge = Nudge(mutation_id="m1", item_ids=("a", "b"), attempt=1)
    assert scope_payload(SCOPE) == {"user_id": "usr-1", "connection_id": "conn-1"}
    assert parse_nudge(scope_payload(SCOPE, nudge)) == nudge
    assert parse_nudge({"user_id": "u"}) is None and parse_nudge({"nudge": {"item_ids": ["a"]}}) is None
