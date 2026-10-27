"""EphemeralStore kit (P11 · G1): the docstring of `src/ports/ephemeral_store.py` checked against
`FirestoreEphemeralStore` on the evaluating Firestore fake (H1), with the index refusal ON (the store makes no query
today; the refusal guards a future one). Assertions are on the raw stored documents and on what readers return.

The one-call marker claim is the caller's (`VoiceCallSetupService.claim`, a get-then-set): it is pinned over this
store in `tests/unit/services/test_voice_call_setup_service_store_pins.py`, because the port promises no conditional
write.

`store` is the one fixture that selects the backend.
"""
import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

import src.adapters.firestore_ephemeral_store as ephemeral_module
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.ports.ephemeral_store import EphemeralStore
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb, EvalDocRef
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
TICKETS = "eval_voice_tickets"
T0 = UTC_MIDNIGHT.utc
TICKET_KEY = "voice_ticket:t1"
MARKER_KEY = "voice_one_call:u1"
TICKET = {"user_id": "u1", "account_id": "a1", "call_kind": "phone"}


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={TICKETS: "development_voice_tickets"})
        self.store: EphemeralStore = FirestoreEphemeralStore(self.db, TICKETS)

    @property
    def col(self):
        return self.db.collection(TICKETS)

    @property
    def docs(self) -> dict:
        return self.col.docs


@pytest.fixture
def store(monkeypatch):
    pin_datetime(monkeypatch, ephemeral_module, T0)
    return Store()


def at(monkeypatch, seconds: float) -> None:
    pin_datetime(monkeypatch, ephemeral_module, T0 + timedelta(seconds=seconds))


def test_the_adapter_implements_the_port():
    assert issubclass(FirestoreEphemeralStore, EphemeralStore)


# ------------------------------------------------------------------------------------------ set / get / expiry

async def test_set_then_get_returns_the_value(store):
    await store.store.set(TICKET_KEY, {"user_id": "u1", "account_id": "a1"}, ttl_s=300)
    assert await store.store.get(TICKET_KEY) == {"user_id": "u1", "account_id": "a1"}


async def test_set_stores_value_and_a_utc_expires_at_for_the_ttl_policy(store):
    await store.store.set(TICKET_KEY, TICKET, ttl_s=300)
    raw = store.docs[TICKET_KEY]
    assert set(raw) == {"value", "expires_at"}
    assert raw["value"] == TICKET
    assert raw["expires_at"].tzinfo is not None and raw["expires_at"] == T0 + timedelta(seconds=300)


async def test_set_replaces_the_value_and_restarts_the_ttl(store, monkeypatch):
    await store.store.set(MARKER_KEY, {"in_flight": True, "call_id": "c1"}, ttl_s=300)
    at(monkeypatch, 200)
    await store.store.set(MARKER_KEY, {"in_flight": True, "call_id": "c2"}, ttl_s=300)
    at(monkeypatch, 400)
    assert await store.store.get(MARKER_KEY) == {"in_flight": True, "call_id": "c2"}


async def test_an_expired_value_reads_as_absent_and_is_deleted(store, monkeypatch):
    await store.store.set(TICKET_KEY, TICKET, ttl_s=300)
    at(monkeypatch, 301)
    assert await store.store.get(TICKET_KEY) is None
    assert TICKET_KEY not in store.docs


@pytest.mark.parametrize("seconds", [299, 300], ids=["one_second_before", "at_expiry"])
async def test_a_value_is_live_up_to_its_expiry_instant(store, monkeypatch, seconds):
    await store.store.set(TICKET_KEY, TICKET, ttl_s=300)
    at(monkeypatch, seconds)
    assert await store.store.get(TICKET_KEY) == TICKET
    assert TICKET_KEY in store.docs


async def test_get_of_an_absent_key_is_none_and_writes_nothing(store):
    clock = store.db._clock
    assert await store.store.get(TICKET_KEY) is None
    assert store.docs == {} and store.col.versions == {} and store.db._clock == clock


# ----------------------------------------------------------------------------------------------------- delete

async def test_delete_removes_and_a_repeat_is_quiet(store):
    await store.store.set(TICKET_KEY, TICKET, ttl_s=300)
    await store.store.delete(TICKET_KEY)
    await store.store.delete(TICKET_KEY)
    assert await store.store.get(TICKET_KEY) is None
    assert store.docs == {}


# --------------------------------------------------------------------------------------------- get_and_delete

async def test_get_and_delete_returns_the_value_once(store):
    await store.store.set(TICKET_KEY, TICKET, ttl_s=300)
    assert await store.store.get_and_delete(TICKET_KEY) == TICKET
    assert await store.store.get_and_delete(TICKET_KEY) is None
    assert store.docs == {}


async def test_get_and_delete_of_an_expired_value_consumes_it_and_returns_none(store, monkeypatch):
    await store.store.set(TICKET_KEY, TICKET, ttl_s=300)
    at(monkeypatch, 301)
    assert await store.store.get_and_delete(TICKET_KEY) is None
    assert store.docs == {}


async def test_get_and_delete_of_an_absent_key_is_none_and_writes_nothing(store):
    assert await store.store.get_and_delete(TICKET_KEY) is None
    # no write stamp on the key: nothing was written (an empty transaction commit still takes a DB-wide stamp in the fake)
    assert store.docs == {} and store.col.versions == {}


async def test_two_concurrent_get_and_delete_exactly_one_wins(store, monkeypatch):
    await store.store.set(TICKET_KEY, TICKET, ttl_s=300)
    reads = []
    real_get = EvalDocRef.get

    async def counting_get(self, transaction=None, field_paths=None):
        if transaction is not None:
            reads.append(self.id)
        return await real_get(self, transaction=transaction, field_paths=field_paths)

    monkeypatch.setattr(EvalDocRef, "get", counting_get)
    store.col.yield_on_get = True
    results = await asyncio.gather(store.store.get_and_delete(TICKET_KEY), store.store.get_and_delete(TICKET_KEY))
    require(len(reads) >= 3, "both bodies read before a commit, and the loser re-read after its abort")
    assert sorted(results, key=lambda r: r is not None) == [None, TICKET]
    assert store.docs == {}


async def _relay_keeps_the_answer(store: EphemeralStore, claims: list) -> None:
    """`_keep_for_chat`: result first, abandoned check second, then the claim."""
    await store.set("voice_delegation_result:t1:call1", {"output": "the garden meeting is on Thursday",
                                                          "user_id": "u1", "account_id": "a1"}, ttl_s=600)
    if await store.get("voice_delegation_abandoned:t1:call1") is not None:
        claims.append(await store.get_and_delete("voice_delegation_result:t1:call1"))


async def _relay_abandons(store: EphemeralStore, claims: list) -> None:
    """`abandon_delegation`: the abandon mark first, then the claim."""
    await store.set("voice_delegation_abandoned:t1:call1", {"abandoned": True}, ttl_s=600)
    claims.append(await store.get_and_delete("voice_delegation_result:t1:call1"))


@pytest.mark.parametrize("order", ["result_first", "abandon_first", "gathered"])
async def test_late_answer_is_claimed_once_in_either_order(store, order):
    claims: list = []
    if order == "result_first":
        await _relay_keeps_the_answer(store.store, claims)
        await _relay_abandons(store.store, claims)
    elif order == "abandon_first":
        await _relay_abandons(store.store, claims)
        await _relay_keeps_the_answer(store.store, claims)
    else:
        store.col.yield_on_get = True
        trace = []

        async def traced(name, flow):
            trace.append(("start", name))
            await flow(store.store, claims)
            trace.append(("end", name))

        await asyncio.gather(traced("keep", _relay_keeps_the_answer), traced("abandon", _relay_abandons))
        require([event for event, _ in trace[:2]] == ["start", "start"], "the two relay flows overlapped")
    assert [c["output"] for c in claims if c is not None] == ["the garden meeting is on Thursday"]
    assert "voice_delegation_result:t1:call1" not in store.docs


# ------------------------------------------------------------------------------------------- tenancy, legacy

async def test_keys_are_global(store):
    """No tenancy: two callers writing the same key share it, the second write wins."""
    await store.store.set(MARKER_KEY, {"in_flight": True, "call_id": "c1"}, ttl_s=300)
    await store.store.set(MARKER_KEY, {"in_flight": True, "call_id": "c2"}, ttl_s=300)
    assert await store.store.get(MARKER_KEY) == {"in_flight": True, "call_id": "c2"}
    assert list(store.docs) == [MARKER_KEY]


@pytest.mark.parametrize("method", ["get", "get_and_delete"])
async def test_an_expired_legacy_document_without_value_reads_as_absent(store, method):
    store.docs[TICKET_KEY] = {"expires_at": T0 - timedelta(seconds=1)}
    assert await getattr(store.store, method)(TICKET_KEY) is None
    assert store.docs == {}


# ---------------------------------------------------------------------------------------------------- errors

@pytest.mark.parametrize("method", ["get", "set", "delete", "get_and_delete"])
async def test_a_backend_error_propagates(store, monkeypatch, method):
    if method != "set":
        await store.store.set(TICKET_KEY, TICKET, ttl_s=300)
    before = {k: dict(v) for k, v in store.docs.items()}

    async def unavailable(self, *args, **kwargs):
        raise RuntimeError("unavailable")

    patched = "get" if method == "get_and_delete" else method
    monkeypatch.setattr(EvalDocRef, patched, unavailable)
    args = (TICKET_KEY, TICKET, 300) if method == "set" else (TICKET_KEY,)
    with pytest.raises(RuntimeError, match="unavailable"):
        await getattr(store.store, method)(*args)
    assert store.docs == before


async def test_a_transaction_aborted_on_every_attempt_raises_value_error(store):
    await store.store.set(TICKET_KEY, TICKET, ttl_s=300)
    for _ in range(5):      # every attempt: another transaction rewrites the document after this one read it
        store.db.interfere_on_txn_read(TICKETS, TICKET_KEY, {"value": dict(TICKET)}, after_read=True)
    with pytest.raises(ValueError):
        await store.store.get_and_delete(TICKET_KEY)
    assert TICKET_KEY in store.docs                 # not consumed
    assert await store.store.get(TICKET_KEY) == TICKET
