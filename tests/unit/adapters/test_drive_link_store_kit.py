"""DriveLinkStore kit: the docstring of `src/ports/drive_link_store.py` checked against `FirestoreDriveLinkStore` on the
evaluating Firestore fake (index refusal on) and against the test fake `tests/support/drive_link_store.py`. Assertions
are on what readers return and, for Firestore, on the raw stored documents.

`backend` is the one fixture that selects the store.
"""
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import AlreadyExists

from src.adapters.firestore_drive_link_store import FirestoreDriveLinkStore
from src.config.environment import EnvironmentConfig
from src.domain.drive_link import LINK_DIGEST_PURPOSE, LINK_PURGE_AFTER_S, LINK_TTL_S, DriveLink
from src.domain.secret_digest import keyed_digest
from src.ports.drive_link_store import DriveLinkStore
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.drive_link_store import FakeDriveLinkStore
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
LINKS = "eval_drive_links"
T0 = UTC_MIDNIGHT.utc
RAW_ID = "Qk7rTz0aB1cD2eF3gH4iJ5"
KEY = keyed_digest(bytes(range(32)), LINK_DIGEST_PURPOSE, RAW_ID)


def link(**changes) -> DriveLink:
    base = dict(link_key=KEY, user_id="u-alex", account_id="acc-alex", connection_id="conn-1", ref_tag="gd",
                item_id="item-fa0137", grant_id="g" * 22, created_at=T0, expires_at=T0 + timedelta(seconds=LINK_TTL_S),
                purge_after=T0 + timedelta(seconds=LINK_PURGE_AFTER_S))
    base.update(changes)
    return DriveLink(**base)


class Firestore:
    name = "firestore"

    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={LINKS: "development_drive_links"})
        self.store: DriveLinkStore = FirestoreDriveLinkStore(self.db, SimpleNamespace(drive_links_collection=LINKS))

    def docs(self) -> dict:
        return self.db.collection(LINKS).docs

    def written(self) -> object:
        return {k: dict(v) for k, v in self.docs().items()}


class Fake:
    name = "fake"

    def __init__(self) -> None:
        self.store = FakeDriveLinkStore()

    def written(self) -> object:
        return dict(self.store.links), list(self.store.writes)


@pytest.fixture(params=[Firestore, Fake], ids=["firestore", "fake"])
def backend(request):
    return request.param()


def test_both_implement_the_port():
    assert issubclass(FirestoreDriveLinkStore, DriveLinkStore) and issubclass(FakeDriveLinkStore, DriveLinkStore)


async def test_create_then_consume_returns_it_once(backend):
    await backend.store.create(link())
    got = await backend.store.consume(KEY, T0 + timedelta(seconds=1), 1)
    assert got == link()
    assert await backend.store.consume(KEY, T0 + timedelta(seconds=2), 1) is None
    assert await backend.store.peek(KEY, T0 + timedelta(seconds=2), 1) is None


async def test_consume_counts_reads_up_to_max(backend):
    await backend.store.create(link())
    first = await backend.store.consume(KEY, T0 + timedelta(seconds=1), 3)
    second = await backend.store.consume(KEY, T0 + timedelta(seconds=2), 3)
    third = await backend.store.consume(KEY, T0 + timedelta(seconds=3), 3)
    assert [first.reads, second.reads, third.reads] == [0, 1, 2]
    assert first.used_at is None and second.used_at == third.used_at == T0 + timedelta(seconds=1)
    assert await backend.store.consume(KEY, T0 + timedelta(seconds=4), 3) is None


async def test_an_expired_link_is_never_consumed_and_nothing_is_written(backend):
    await backend.store.create(link())
    before = backend.written()
    assert await backend.store.consume(KEY, T0 + timedelta(seconds=LINK_TTL_S), 1) is None
    assert await backend.store.peek(KEY, T0 + timedelta(seconds=LINK_TTL_S), 1) is None
    assert await backend.store.consume("absent-key", T0, 1) is None
    assert backend.written() == before


async def test_peek_never_writes(backend):
    await backend.store.create(link())
    before = backend.written()
    assert await backend.store.peek(KEY, T0 + timedelta(seconds=1), 1) == link()
    assert await backend.store.peek(KEY, T0 + timedelta(seconds=2), 1) == link()
    assert backend.written() == before
    assert (await backend.store.consume(KEY, T0 + timedelta(seconds=3), 1)).reads == 0


async def test_two_concurrent_consumes_one_wins(backend):
    await backend.store.create(link())
    if isinstance(backend, Firestore):
        # another transaction commits the one read right after this one read the document: its commit aborts and
        # the re-run finds the link spent
        backend.db.interfere_on_txn_read(LINKS, KEY, {"reads": 1, "used_at": T0}, after_read=True)
        assert await backend.store.consume(KEY, T0 + timedelta(seconds=1), 1) is None
        assert backend.docs()[KEY]["reads"] == 1
        await backend.store.create(link(link_key="k2"))
        results = await asyncio.gather(backend.store.consume("k2", T0, 1), backend.store.consume("k2", T0, 1))
        assert sum(r is not None for r in results) == 1
        assert backend.docs()["k2"]["reads"] == 1
        return
    results = await asyncio.gather(backend.store.consume(KEY, T0, 1), backend.store.consume(KEY, T0, 1))
    assert sum(r is not None for r in results) == 1


async def test_create_never_overwrites(backend):
    await backend.store.create(link())
    with pytest.raises(AlreadyExists):
        await backend.store.create(link(item_id="another-item"))
    assert (await backend.store.peek(KEY, T0, 1)).item_id == "item-fa0137"


async def test_the_stored_document():
    fs = Firestore()
    await fs.store.create(link())
    await fs.store.consume(KEY, T0 + timedelta(seconds=7), 1)
    assert fs.docs() == {KEY: {
        "user_id": "u-alex", "account_id": "acc-alex", "connection_id": "conn-1", "ref_tag": "gd",
        "item_id": "item-fa0137", "grant_id": "g" * 22, "created_at": T0, "expires_at": T0 + timedelta(seconds=300),
        "purge_after": T0 + timedelta(seconds=86_400), "reads": 1, "used_at": T0 + timedelta(seconds=7),
    }}
    assert RAW_ID not in repr(fs.docs())


async def test_the_collection_carries_the_env_prefix(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    env = EnvironmentConfig()
    db = EvalDb()
    await FirestoreDriveLinkStore(db, env).create(link())
    assert env.drive_links_collection == "development_drive_links"
    assert list(db.collection("development_drive_links").docs) == [KEY]
