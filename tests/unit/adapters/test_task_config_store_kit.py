"""TaskConfigPort kit (P11, G3 store 2): the docstring of `src/ports/task_config_port.py` checked against
`FirestoreTaskConfigRepository` on the evaluating Firestore fake (H1), with the index refusal ON (the adapter issues no
query, so nothing is refused). Assertions are on the raw stored documents and on what the readers return.

The overwrite defect that the store's full-replace contract exposes sits in `TaskSetupService.ensure_subscriptions`
(P11-G3-3); it is pinned through the real service in `tests/unit/services/test_task_setup_service_store_pins.py`.

`_adapter(db)` is the one place the adapter is constructed.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.domain.task import TaskSubscriptionConfig, TaskUserConfig
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
COL = "eval_task_config"
EXPIRES = datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc)


def _adapter(db: EvalDb) -> FirestoreTaskConfigRepository:
    return FirestoreTaskConfigRepository(db, SimpleNamespace(task_config_collection=COL))


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE)
        self.repo = _adapter(self.db)

    @property
    def docs(self) -> dict:
        return self.db.collection(COL).docs


@pytest.fixture
def store():
    return Store()


def sub(sub_id="s1", list_id="L1", expires_at=EXPIRES) -> TaskSubscriptionConfig:
    return TaskSubscriptionConfig(sub_id=sub_id, list_id=list_id, expires_at=expires_at)


# ------------------------------------------------------------------------------------------------- get / save

async def test_a_missing_document_reads_as_an_empty_config(store):
    got = await store.repo.get_config("u1")
    assert (got.primary_list_id, got.subscriptions) == (None, [])
    assert store.docs == {}


async def test_save_then_get_round_trips_and_reads_expiry_as_naive_utc(store):
    plus2 = timezone(timedelta(hours=2))
    local = datetime(2026, 11, 3, 10, 30, tzinfo=plus2)          # the same instant as EXPIRES
    await store.repo.save_config("u1", TaskUserConfig(primary_list_id="L1", subscriptions=[sub(expires_at=local)]))
    stored = store.docs["u1"]
    assert stored["primary_list_id"] == "L1"
    assert stored["subscriptions"] == [{"sub_id": "s1", "list_id": "L1", "expires_at": EXPIRES}]
    assert stored["subscriptions"][0]["expires_at"].tzinfo is not None
    got = await store.repo.get_config("u1")
    assert got.primary_list_id == "L1"
    assert [(s.sub_id, s.list_id, s.expires_at) for s in got.subscriptions] == [("s1", "L1", datetime(2026, 11, 3, 8, 30))]
    assert got.subscriptions[0].expires_at.tzinfo is None


async def test_a_stored_datetime_with_nanoseconds_reads_as_a_naive_plain_datetime(store):
    stored = DatetimeWithNanoseconds(2026, 11, 3, 8, 30, 15, 250000, tzinfo=timezone.utc)
    store.docs["u1"] = {"primary_list_id": "L1", "subscriptions": [{"sub_id": "s1", "list_id": "L1", "expires_at": stored}]}
    got = (await store.repo.get_config("u1")).subscriptions[0].expires_at
    assert (type(got), got.tzinfo, got) == (datetime, None, datetime(2026, 11, 3, 8, 30, 15, 250000))


async def test_save_config_replaces_the_whole_document_a_none_list_id_erases_the_stored_one(store):
    await store.repo.save_config("u1", TaskUserConfig(primary_list_id="L1", subscriptions=[sub("s1")]))
    await store.repo.save_config("u1", TaskUserConfig(subscriptions=[sub("s2", "L2")]))
    stored = store.docs["u1"]
    assert stored["primary_list_id"] is None
    assert [s["sub_id"] for s in stored["subscriptions"]] == ["s2"]


async def test_save_config_twice_stores_the_same_document(store):
    config = TaskUserConfig(primary_list_id="L1", subscriptions=[sub()])
    await store.repo.save_config("u1", config)
    first = dict(store.docs["u1"])
    await store.repo.save_config("u1", config)
    assert store.docs["u1"] == first and list(store.docs) == ["u1"]


async def test_configs_are_per_user(store):
    await store.repo.save_config("u1", TaskUserConfig(primary_list_id="L1", subscriptions=[sub("s1")]))
    await store.repo.save_config("u2", TaskUserConfig(primary_list_id="L2", subscriptions=[sub("s2", "L2")]))
    assert (await store.repo.get_config("u1")).primary_list_id == "L1"
    assert [s.sub_id for s in (await store.repo.get_config("u2")).subscriptions] == ["s2"]
    assert (await store.repo.get_config("u3")).primary_list_id is None


async def test_a_legacy_document_without_fields_reads_empty(store):
    store.docs["u1"] = {}
    store.docs["u2"] = {"primary_list_id": "L1"}
    store.docs["u3"] = {"subscriptions": [{"sub_id": "s1", "list_id": "L1", "expires_at": EXPIRES}]}
    empty, only_list, only_subs = [await store.repo.get_config(u) for u in ("u1", "u2", "u3")]
    assert (empty.primary_list_id, empty.subscriptions) == (None, [])
    assert (only_list.primary_list_id, only_list.subscriptions) == ("L1", [])
    assert (only_subs.primary_list_id, [s.sub_id for s in only_subs.subscriptions]) == (None, ["s1"])


@pytest.mark.parametrize("missing", ["sub_id", "list_id"])
async def test_a_subscription_without_its_ids_raises_key_error(store, missing):
    entry = {"sub_id": "s1", "list_id": "L1", "expires_at": EXPIRES}
    del entry[missing]
    store.docs["u1"] = {"primary_list_id": "L1", "subscriptions": [entry]}
    with pytest.raises(KeyError):
        await store.repo.get_config("u1")


# -------------------------------------------------------------------------- set_primary_list_id_if_absent

async def test_set_primary_writes_when_absent_and_keeps_the_subscriptions(store):
    store.docs["u1"] = {"primary_list_id": None, "subscriptions": [{"sub_id": "s1", "list_id": "L1", "expires_at": EXPIRES}]}
    assert await store.repo.set_primary_list_id_if_absent("u1", "LZ") == "LZ"
    assert store.docs["u1"]["primary_list_id"] == "LZ"
    assert [s["sub_id"] for s in store.docs["u1"]["subscriptions"]] == ["s1"]


async def test_set_primary_creates_the_document_when_there_is_none(store):
    assert await store.repo.set_primary_list_id_if_absent("u1", "LZ") == "LZ"
    assert store.docs["u1"] == {"primary_list_id": "LZ"}


async def test_set_primary_returns_the_stored_value_and_leaves_the_document_unchanged(store):
    store.docs["u1"] = {"primary_list_id": "L1", "subscriptions": []}
    assert await store.repo.set_primary_list_id_if_absent("u1", "L2") == "L1"
    assert store.docs["u1"] == {"primary_list_id": "L1", "subscriptions": []}


async def test_set_primary_when_another_caller_commits_first_returns_that_callers_value(store):
    """The first commit wins: the list id written between this caller's read and its commit is returned and kept."""
    store.docs["u1"] = {"primary_list_id": None, "subscriptions": []}
    store.db.interfere_on_txn_read(COL, "u1", {"primary_list_id": "LB"}, after_read=True)
    assert await store.repo.set_primary_list_id_if_absent("u1", "LA") == "LB"
    assert store.docs["u1"]["primary_list_id"] == "LB"


async def test_set_primary_is_per_user(store):
    assert await store.repo.set_primary_list_id_if_absent("u1", "L1") == "L1"
    assert await store.repo.set_primary_list_id_if_absent("u2", "L2") == "L2"
    assert [store.docs[u]["primary_list_id"] for u in ("u1", "u2")] == ["L1", "L2"]


# ----------------------------------------------------------------------------------------------- P11-G3-2

@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "P11-G3-2: a subscription without expires_at reads as an aware now, every caller compares with a naive now "
    "(a code defect whatever the stored data holds: the fallback exists only for this case)"))
async def test_a_subscription_without_expires_at_reads_naive(store):
    """Every expires_at the store returns is naive UTC, whatever the stored document lacks. Flipping fix
    (`firestore_task_config_repository.py` `_sub_from_dict`): `expires_at or datetime.now(timezone.utc).replace(tzinfo=None)`;
    a fix that drops such a subscription passes too."""
    store.docs["u1"] = {"primary_list_id": "L1", "subscriptions": [{"sub_id": "s1", "list_id": "L1"}]}
    cfg = await store.repo.get_config("u1")
    require(cfg.primary_list_id == "L1", "the stored config is read")
    assert all(s.expires_at.tzinfo is None for s in cfg.subscriptions), (
        f"read back {[s.expires_at for s in cfg.subscriptions]}")


async def test_a_subscription_with_expires_at_reads_naive(store):
    """The passing control of P11-G3-2."""
    await store.repo.save_config("u1", TaskUserConfig(primary_list_id="L1", subscriptions=[sub()]))
    cfg = await store.repo.get_config("u1")
    assert all(s.expires_at.tzinfo is None for s in cfg.subscriptions) and len(cfg.subscriptions) == 1
