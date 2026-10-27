"""LongTurnRegistry kit (P11 · G1): the docstring of `src/ports/long_turn_registry.py` checked against
`FirestoreLongTurnRegistry` on the evaluating Firestore fake (H1), with the index refusal ON (`list_running` is two
equalities). Assertions are on the raw stored documents and on what the registry returns.

`started_at` and `heartbeat_at` are epoch floats from `time.time()`, which `pin_datetime` does not pin: records are
seeded relative to `time.time()` and to `STALE_AFTER_S`, and no elapsed time is measured.
"""
import asyncio
import time
from datetime import timedelta
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound
from pydantic import ValidationError

import src.adapters.firestore_long_turn_registry as registry_module
from src.adapters.firestore_long_turn_registry import FirestoreLongTurnRegistry
from src.domain.long_turn import LongTurnRecord, LongTurnStatus
from src.domain.turn_clock import STALE_AFTER_S
from src.ports.long_turn_registry import LongTurnRegistry
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb, EvalDocRef
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
TURNS = "eval_long_turns"
T0 = UTC_MIDNIGHT.utc + timedelta(hours=9)
TITLE = "plan the weekend in Coimbra"


class Turns:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={TURNS: "development_long_turns"})
        self.registry: LongTurnRegistry = FirestoreLongTurnRegistry(self.db, TURNS)

    @property
    def col(self):
        return self.db.collection(TURNS)

    @property
    def docs(self) -> dict:
        return self.col.docs


@pytest.fixture
def turns(monkeypatch):
    pin_datetime(monkeypatch, registry_module, T0)
    return Turns()


def record(turn_id="turn1", user_id="u1", heartbeat_age=5.0, **over) -> LongTurnRecord:
    now = time.time()
    fields = dict(turn_id=turn_id, user_id=user_id, session_id=f"{user_id}:C1", title=TITLE, started_at=now - 120,
                  heartbeat_at=now - heartbeat_age)
    fields.update(over)
    return LongTurnRecord(**fields)


def test_the_adapter_implements_the_port():
    assert issubclass(FirestoreLongTurnRegistry, LongTurnRegistry)


async def test_start_stores_the_record_json_shaped_with_a_ttl_field(turns):
    rec = record()
    await turns.registry.start(rec)
    raw = turns.docs["turn1"]
    assert raw["status"] == "running" and raw["cancel_requested"] is False and raw["step"] == "starting"
    assert isinstance(raw["started_at"], float) and isinstance(raw["heartbeat_at"], float)
    assert raw["expires_at"] == T0 + timedelta(days=2) and raw["expires_at"].tzinfo is not None
    assert {k: v for k, v in raw.items() if k != "expires_at"} == rec.model_dump(mode="json")


async def test_start_replaces_a_record_of_the_same_turn(turns):
    await turns.registry.start(record(status=LongTurnStatus.DONE, cancel_requested=True, step="writing"))
    await turns.registry.start(record())
    raw = turns.docs["turn1"]
    assert (raw["status"], raw["cancel_requested"], raw["step"]) == ("running", False, "starting")


async def test_get_round_trips_and_leaves_out_expires_at(turns):
    rec = record()
    await turns.registry.start(rec)
    assert await turns.registry.get("turn1") == rec


async def test_get_of_an_absent_turn_is_none(turns):
    assert await turns.registry.get("turn1") is None


async def test_heartbeat_updates_time_and_step_and_returns_the_cancel_flag(turns):
    await turns.registry.start(record(heartbeat_age=60))
    before = turns.docs["turn1"]["heartbeat_at"]
    assert await turns.registry.heartbeat("turn1", "searching") is False
    assert turns.docs["turn1"]["step"] == "searching" and turns.docs["turn1"]["heartbeat_at"] > before
    assert await turns.registry.request_cancel("u1", "turn1") is True
    assert await turns.registry.heartbeat("turn1", "writing") is True
    assert turns.docs["turn1"]["step"] == "writing"


@pytest.mark.parametrize("method", ["heartbeat", "finish"])
async def test_heartbeat_and_finish_of_a_missing_turn_raise_not_found(turns, method):
    arg = "searching" if method == "heartbeat" else LongTurnStatus.DONE
    with pytest.raises(NotFound):
        await getattr(turns.registry, method)("turn1", arg)
    assert turns.docs == {}


async def test_finish_sets_status_and_keeps_the_cancel_flag(turns):
    await turns.registry.start(record(cancel_requested=True))
    before = dict(turns.docs["turn1"])
    await turns.registry.finish("turn1", LongTurnStatus.CANCELLED)
    after = turns.docs["turn1"]
    assert after["status"] == "cancelled" and after["cancel_requested"] is True
    assert {k: v for k, v in after.items() if k != "status"} == {k: v for k, v in before.items() if k != "status"}


async def test_list_running_is_the_users_live_running_turns(turns):
    await turns.registry.start(record("live"))
    await turns.registry.start(record("done", status=LongTurnStatus.DONE))
    await turns.registry.start(record("stale", heartbeat_age=STALE_AFTER_S + 5))
    await turns.registry.start(record("other", user_id="u2"))
    assert [r.turn_id for r in await turns.registry.list_running("u1")] == ["live"]


async def test_list_running_raises_on_an_unreadable_running_record(turns):
    await turns.registry.start(record("live"))
    turns.docs["broken"] = {"turn_id": "broken", "user_id": "u1", "session_id": "u1:C1", "status": "running",
                            "started_at": time.time(), "heartbeat_at": time.time()}       # no title
    with pytest.raises(ValidationError):
        await turns.registry.list_running("u1")


async def test_list_running_never_lists_an_unknown_status(turns):
    turns.docs["paused"] = {**record("paused").model_dump(mode="json"), "status": "paused"}
    assert await turns.registry.list_running("u1") == []


async def test_get_raises_on_an_unreadable_record(turns):
    turns.docs["paused"] = {**record("paused").model_dump(mode="json"), "status": "paused"}
    with pytest.raises(ValidationError):
        await turns.registry.get("paused")


async def test_request_cancel_sets_the_flag_on_the_users_running_turn(turns):
    await turns.registry.start(record())
    assert await turns.registry.request_cancel("u1", "turn1") is True
    assert turns.docs["turn1"]["cancel_requested"] is True


@pytest.mark.parametrize("case", ["foreign", "finished", "absent"])
async def test_request_cancel_refuses_foreign_finished_absent(turns, case):
    await turns.registry.start(record(status=LongTurnStatus.DONE if case == "finished" else LongTurnStatus.RUNNING))
    before = dict(turns.docs["turn1"])
    user_id, turn_id = {"foreign": ("u2", "turn1"), "finished": ("u1", "turn1"), "absent": ("u1", "turn2")}[case]
    assert await turns.registry.request_cancel(user_id, turn_id) is False
    assert turns.docs == {"turn1": before}


async def test_a_cancel_racing_a_finish_still_returns_true(turns):
    await turns.registry.start(record())
    turns.col.yield_on_get = True      # the cancel reads "running", the finish lands, then the cancel writes its flag
    events = []

    async def cancel():
        granted = await turns.registry.request_cancel("u1", "turn1")
        events.append("cancel")
        return granted

    async def finish():
        await turns.registry.finish("turn1", LongTurnStatus.DONE)
        events.append("finish")

    granted, _ = await asyncio.gather(cancel(), finish())
    require(events == ["finish", "cancel"], "the finish landed between the cancel's read and its write")
    assert granted is True
    assert turns.docs["turn1"]["status"] == "done" and turns.docs["turn1"]["cancel_requested"] is True


async def test_index_refusal_is_on_and_list_running_needs_no_composite(turns):
    assert turns.db.declared is not None
    await turns.registry.start(record())
    assert len(await turns.registry.list_running("u1")) == 1
    assert turns.col.executed[-1] == [("where", "user_id", "==", "u1"), ("where", "status", "==", "running")]


async def test_a_backend_error_in_list_running_propagates(turns):
    await turns.registry.start(record())
    turns.col.fail = RuntimeError("unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        await turns.registry.list_running("u1")


@pytest.mark.parametrize("method", ["get", "request_cancel"])
async def test_a_backend_error_on_a_read_propagates(turns, monkeypatch, method):
    await turns.registry.start(record())
    before = dict(turns.docs["turn1"])

    async def unavailable(self, *args, **kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(EvalDocRef, "get", unavailable)
    args = ("turn1",) if method == "get" else ("u1", "turn1")
    with pytest.raises(RuntimeError, match="unavailable"):
        await getattr(turns.registry, method)(*args)
    assert turns.docs["turn1"] == before
