"""The memory inbox's `/worker` task keys and payload (MCP_MEMORY_SUBMISSIONS_RFC §10)."""
from datetime import datetime, timezone

from src.domain.memory_inbox_task_keys import (
    INBOX_DEADLINE_S,
    final_defer_key,
    final_key,
    task_payload,
    triage_defer_key,
    triage_key,
)
from src.domain.memory_write_lease import defer_bucket

UTC = timezone.utc


def test_the_keys():
    now = datetime(2026, 11, 25, 23, 30, tzinfo=UTC)
    assert triage_key("u1", now, "Europe/Madrid") == "memory-inbox-triage:u1:2026-11-26"
    assert triage_defer_key("a1", now) == f"memory-inbox-triage-defer:a1:{defer_bucket(now)}"
    assert final_key("u1", now) == f"memory-inbox-final:u1:{int(now.timestamp())}"
    assert final_defer_key("a1", now) == f"memory-inbox-final-defer:a1:{defer_bucket(now)}"
    assert task_payload("u1") == {"user_id": "u1"} and INBOX_DEADLINE_S == 1800


def test_the_triage_key_counts_the_owners_day_far_from_utc():      # Review Focus 4
    now = datetime(2026, 11, 25, 11, 30, tzinfo=UTC)
    assert triage_key("u1", now, "Pacific/Auckland") == "memory-inbox-triage:u1:2026-11-26"
