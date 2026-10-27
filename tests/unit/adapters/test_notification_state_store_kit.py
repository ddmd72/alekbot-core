"""NotificationStatePort kit (P11 · G2): the docstring of `src/ports/notification_state_port.py` checked against
`FirestoreNotificationStateAdapter` on the evaluating Firestore fake (H1). The adapter runs no query; the index refusal is
on anyway, for uniformity. Assertions are on the raw stored documents and on what readers return.
"""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.adapters.firestore_notification_state_adapter as state_module
from src.adapters.firestore_notification_state_adapter import FirestoreNotificationStateAdapter
from src.domain.notification import NotificationChannel
from tests.support.clock_instants import pin_datetime
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
LAST, PRIMARY = "eval_user_notification_state", "eval_user_primary_channel"
T0 = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 27, 9, 15, tzinfo=timezone.utc)


@pytest.fixture
def state(monkeypatch):
    pin_datetime(monkeypatch, state_module, T0)
    env = SimpleNamespace(user_notification_state_collection=LAST, firestore_collection_prefix="eval_")
    return FirestoreNotificationStateAdapter(EvalDb(indexes=INDEXES_FILE), env)


def docs(state, name=LAST):
    return state.db.collection(name).docs


async def test_save_then_get_round_trip_naive_utc(state):
    await state.save("u1", "slack", "C1")
    assert docs(state)["u1"] == {"user_id": "u1", "platform": "slack", "channel_id": "C1", "updated_at": T0}
    assert await state.get("u1") == NotificationChannel(user_id="u1", platform="slack", channel_id="C1",
                                                        updated_at=T0.replace(tzinfo=None))


async def test_a_second_save_replaces_the_first(state, monkeypatch):
    await state.save("u1", "slack", "C1")
    pin_datetime(monkeypatch, state_module, T1)
    await state.save("u1", "telegram", "777")
    got = await state.get("u1")
    assert (got.platform, got.channel_id, got.updated_at) == ("telegram", "777", T1.replace(tzinfo=None))


async def test_primary_lives_in_its_own_collection_and_is_none_until_set(state):
    await state.save("u1", "slack", "C1")
    assert await state.get_primary("u1") is None
    await state.save_primary("u1", "slack", "C2")
    assert docs(state, PRIMARY)["u1"] == {"user_id": "u1", "platform": "slack", "channel_id": "C2", "updated_at": T0}
    assert ((await state.get_primary("u1")).channel_id, (await state.get("u1")).channel_id) == ("C2", "C1")


async def test_get_missing_is_none(state):
    assert await state.get("u9") is None and await state.get_primary("u9") is None


async def test_users_are_independent(state):
    await state.save("u1", "slack", "C1")
    await state.save("u2", "telegram", "777")
    await state.save_primary("u2", "telegram", "777")
    assert (await state.get("u1")).channel_id == "C1" and await state.get_primary("u1") is None
    assert (await state.get("u2")).channel_id == "777"


async def test_missing_updated_at_reads_as_now_aware(state):
    docs(state)["u1"] = {"user_id": "u1", "platform": "slack", "channel_id": "C1"}
    docs(state, PRIMARY)["u1"] = {"user_id": "u1", "platform": "slack", "channel_id": "C2", "updated_at": None}
    assert (await state.get("u1")).updated_at == T0
    assert (await state.get_primary("u1")).updated_at == T0


async def test_a_record_missing_a_required_field_raises(state):
    docs(state)["u1"] = {"user_id": "u1", "platform": "slack", "updated_at": T0}
    docs(state, PRIMARY)["u1"] = {"user_id": "u1", "channel_id": "C2", "updated_at": T0}
    raised = []
    for read in (state.get("u1"), state.get_primary("u1")):
        try:
            await read
        except KeyError as exc:
            raised.append(exc.args[0])
    assert raised == ["channel_id", "platform"]
