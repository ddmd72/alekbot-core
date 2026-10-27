"""ShortLinkRepositoryPort kit (P11, G3 store 8): the docstring of `src/ports/short_link_repository_port.py` checked against
`FirestoreShortLinkRepository` on the evaluating Firestore fake (H1), with the index refusal ON (the adapter issues no
query). Assertions are on the raw stored documents and on what `resolve` returns.

`create` is atomic by construction on both the fake and Firestore, so there is no concurrent variant of the taken-code
test: the second create of a code is refused whenever it comes.

`_adapter(db)` is the one place the adapter is constructed.
"""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters import firestore_short_link_repository
from src.adapters.firestore_short_link_repository import FirestoreShortLinkRepository
from src.domain.short_link import ShortLink
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
COL = "eval_short_links"
NOW = UTC_MIDNIGHT.utc
TARGET = "https://example.com/f/birth-certificate"


def _adapter(db: EvalDb) -> FirestoreShortLinkRepository:
    return FirestoreShortLinkRepository(db, SimpleNamespace(firestore_collection_prefix="eval_"))


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE)
        self.repo = _adapter(self.db)

    @property
    def docs(self) -> dict:
        return self.db.collection(COL).docs


@pytest.fixture
def store(monkeypatch):
    pin_datetime(monkeypatch, firestore_short_link_repository, NOW)
    return Store()


def link(code="ab3xk9", target=TARGET, expires_at=NOW + timedelta(days=30)) -> ShortLink:
    return ShortLink(code=code, target_url=target, expires_at=expires_at)


async def test_a_created_link_is_stored_under_its_code_and_resolves(store):
    assert await store.repo.create_if_absent(link()) is True
    assert store.docs == {"ab3xk9": {"target_url": TARGET, "expires_at": NOW + timedelta(days=30)}}
    assert await store.repo.resolve("ab3xk9") == link()


async def test_a_second_create_of_a_taken_code_is_refused_and_the_first_link_is_kept(store):
    assert await store.repo.create_if_absent(link(target="https://example.com/f/first")) is True
    assert await store.repo.create_if_absent(link(target="https://example.com/f/second")) is False
    assert store.docs["ab3xk9"]["target_url"] == "https://example.com/f/first"
    assert (await store.repo.resolve("ab3xk9")).target_url == "https://example.com/f/first"


async def test_two_codes_are_two_links(store):
    assert await store.repo.create_if_absent(link("aaa111", target="https://example.com/f/one")) is True
    assert await store.repo.create_if_absent(link("bbb222", target="https://example.com/f/two")) is True
    assert (await store.repo.resolve("aaa111")).target_url == "https://example.com/f/one"
    assert (await store.repo.resolve("bbb222")).target_url == "https://example.com/f/two"


async def test_an_unknown_code_resolves_to_none(store):
    await store.repo.create_if_absent(link())
    assert await store.repo.resolve("zzzzzz") is None
    assert await store.repo.resolve("AB3XK9") is None            # the code is matched exactly


async def test_a_link_expires_at_its_expiry_instant_and_not_a_second_before(store, monkeypatch):
    await store.repo.create_if_absent(link(expires_at=NOW + timedelta(hours=1)))
    pin_datetime(monkeypatch, firestore_short_link_repository, NOW + timedelta(hours=1) - timedelta(seconds=1))
    assert (await store.repo.resolve("ab3xk9")).target_url == TARGET
    pin_datetime(monkeypatch, firestore_short_link_repository, NOW + timedelta(hours=1))
    assert await store.repo.resolve("ab3xk9") is None
    assert "ab3xk9" in store.docs                                  # resolving never deletes: the backend's TTL sweep does


async def test_a_code_stays_taken_after_its_link_expired_until_the_backend_removes_it(store, monkeypatch):
    await store.repo.create_if_absent(link(expires_at=NOW + timedelta(hours=1)))
    pin_datetime(monkeypatch, firestore_short_link_repository, NOW + timedelta(hours=2))
    assert await store.repo.create_if_absent(link(target="https://example.com/f/new")) is False
    assert await store.repo.resolve("ab3xk9") is None


