"""Wire tests at the Firestore client boundary."""
from unittest.mock import AsyncMock, MagicMock

from src.adapters.firestore_long_turn_registry import FirestoreLongTurnRegistry
from src.domain.long_turn import LongTurnRecord, LongTurnStatus


def _db():
    db = MagicMock()
    doc = MagicMock()
    doc.set = AsyncMock()
    doc.update = AsyncMock()
    snap = MagicMock()
    snap.exists = True
    snap.to_dict.return_value = {
        "turn_id": "slack:Ev1", "user_id": "u1", "session_id": "u1:D1", "title": "t",
        "started_at": 0.0, "heartbeat_at": 1.0, "step": "thinking",
        "status": "running", "cancel_requested": True, "expires_at": "ignored",
    }
    doc.get = AsyncMock(return_value=snap)
    db.collection.return_value.document.return_value = doc
    return db, doc


async def test_start_writes_record_with_ttl_field():
    db, doc = _db()
    reg = FirestoreLongTurnRegistry(db, "development_long_turns")
    rec = LongTurnRecord(turn_id="slack:Ev1", user_id="u1", session_id="u1:D1",
                         title="t", started_at=0.0, heartbeat_at=0.0)
    await reg.start(rec)
    db.collection.assert_called_with("development_long_turns")
    written = doc.set.call_args.args[0]
    assert written["status"] == "running"
    assert "expires_at" in written


async def test_get_parses_record_and_ignores_ttl_field():
    db, _ = _db()
    rec = await FirestoreLongTurnRegistry(db, "c").get("slack:Ev1")
    assert rec.turn_id == "slack:Ev1" and rec.status is LongTurnStatus.RUNNING


async def test_heartbeat_updates_and_returns_cancel_flag():
    db, doc = _db()
    cancel = await FirestoreLongTurnRegistry(db, "c").heartbeat("slack:Ev1", "tool: search_web")
    assert cancel is True
    fields = doc.update.call_args.args[0]
    assert fields["step"] == "tool: search_web" and "heartbeat_at" in fields


async def test_finish_sets_status():
    db, doc = _db()
    await FirestoreLongTurnRegistry(db, "c").finish("slack:Ev1", LongTurnStatus.DONE)
    assert doc.update.call_args.args[0] == {"status": "done"}


async def test_request_cancel_only_for_own_running_turn():
    db, doc = _db()
    reg = FirestoreLongTurnRegistry(db, "c")
    assert await reg.request_cancel("u1", "slack:Ev1") is True
    assert doc.update.call_args.args[0] == {"cancel_requested": True}
    assert await reg.request_cancel("someone-else", "slack:Ev1") is False


# --- Final review I7: list_running drops stale records ------------------------------


def _streaming_db(records):
    db = MagicMock()
    snaps = []
    for rec in records:
        snap = MagicMock()
        snap.to_dict.return_value = {**rec, "expires_at": "ignored"}
        snaps.append(snap)

    async def stream():
        for snap in snaps:
            yield snap
    query = MagicMock()
    query.where.return_value = query
    query.stream = stream
    db.collection.return_value.where.return_value = query
    return db


def _row(turn_id, heartbeat_at):
    return {"turn_id": turn_id, "user_id": "u1", "session_id": "u1:D1", "title": "t",
            "started_at": 0.0, "heartbeat_at": heartbeat_at, "step": "thinking",
            "status": "running", "cancel_requested": False}


async def test_list_running_returns_only_records_with_a_fresh_heartbeat():
    import time
    from src.domain.turn_clock import STALE_AFTER_S
    now = time.time()
    db = _streaming_db([_row("fresh", now - 5), _row("dead", now - STALE_AFTER_S - 60)])
    out = await FirestoreLongTurnRegistry(db, "c").list_running("u1")
    assert [r.turn_id for r in out] == ["fresh"]
