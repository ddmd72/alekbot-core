"""WhitelistRepository kit (P6 · T2): the docstring of `src/ports/whitelist_repository.py` checked against
`FirestoreWhitelistRepository` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on what
`get_whitelist` returns and on the access decision callers take from it (`WhitelistEntry.is_allowed`).

Only `get_whitelist` has a caller in `src/`; add_email / remove_email / add_domain / remove_domain are marked "no caller in
src/" and are not tested. The stored config is therefore seeded as a raw document, the way the admin script writes it.

`store` is the one fixture that selects the backend.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.domain.whitelist import WhitelistEntry
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
WHITELIST = "eval_domain_whitelist_v1"


class WhitelistStore:
    """The real repository over one EvalDb with the declared indexes, its collection mapped to its index group."""

    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={WHITELIST: "development_domain_whitelist_v1"})
        self.repo = FirestoreWhitelistRepository(self.db, SimpleNamespace(domain_whitelist_collection=WHITELIST))

    @property
    def docs(self) -> dict:
        return self.db.collection(WHITELIST).docs


@pytest.fixture
def store():
    return WhitelistStore()


def seed(store, emails=None, domains=None, doc_id="config", **extra):
    doc = dict(extra)
    if emails is not None:
        doc["allowed_emails"] = list(emails)
    if domains is not None:
        doc["allowed_domains"] = list(domains)
    store.docs[doc_id] = doc


# ------------------------------------------------------------------------------------------- what it returns

async def test_returns_the_stored_emails_and_domains(store):
    seed(store, emails=["clara@example.com", "robin@example.net"], domains=["example.org"])
    entry = await store.repo.get_whitelist()
    assert isinstance(entry, WhitelistEntry)
    assert entry.allowed_emails == {"clara@example.com", "robin@example.net"}
    assert entry.allowed_domains == {"example.org"}


async def test_an_allowed_email_and_an_email_of_an_allowed_domain_pass_and_nothing_else_does(store):
    seed(store, emails=["clara@example.com"], domains=["example.org"])
    entry = await store.repo.get_whitelist()
    assert entry.is_allowed("clara@example.com") and entry.is_allowed("Clara@Example.com ")
    assert entry.is_allowed("hanna@example.org")
    assert not entry.is_allowed("robin@example.com")
    assert not entry.is_allowed("clara@example.com.evil.test")
    assert not entry.is_allowed("eve@sub.example.org")


async def test_no_cache_a_removed_email_is_refused_on_the_next_call(store):
    seed(store, emails=["clara@example.com", "robin@example.com"], domains=[])
    assert (await store.repo.get_whitelist()).is_allowed("clara@example.com")
    seed(store, emails=["robin@example.com"], domains=[])            # the admin removes Clara
    after = await store.repo.get_whitelist()
    assert not after.is_allowed("clara@example.com")
    assert after.is_allowed("robin@example.com")


async def test_no_cache_a_removed_domain_is_refused_on_the_next_call(store):
    seed(store, emails=[], domains=["example.org"])
    assert (await store.repo.get_whitelist()).is_allowed("hanna@example.org")
    seed(store, emails=[], domains=[])
    assert not (await store.repo.get_whitelist()).is_allowed("hanna@example.org")


async def test_no_cache_an_added_email_passes_on_the_next_call(store):
    seed(store, emails=[], domains=[])
    assert not (await store.repo.get_whitelist()).is_allowed("clara@example.com")
    seed(store, emails=["clara@example.com"], domains=[])
    assert (await store.repo.get_whitelist()).is_allowed("clara@example.com")


async def test_the_read_does_not_change_the_stored_config(store):
    seed(store, emails=["clara@example.com"], domains=["example.org"], updated_at="kept")
    before = dict(store.docs["config"])
    await store.repo.get_whitelist()
    assert store.docs == {"config": before}


# ------------------------------------------------------------------------------------- missing config: no one

async def test_missing_config_is_an_empty_whitelist_and_no_one_is_allowed(store):
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == (set(), set())
    assert not entry.is_allowed("clara@example.com")
    assert store.docs == {}                                         # reading creates nothing


async def test_only_the_config_document_is_read(store):
    """Tenancy: one global whitelist, the singleton `config`; any other document in the collection grants nothing."""
    seed(store, emails=["clara@example.com"], domains=["example.org"], doc_id="acc-1")
    seed(store, emails=["eve@example.com"], domains=["evil.test"], doc_id="Config")
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == (set(), set())
    seed(store, emails=["robin@example.com"], domains=[])
    entry = await store.repo.get_whitelist()
    assert entry.allowed_emails == {"robin@example.com"} and entry.allowed_domains == set()


# ------------------------------------------------------------------------------------------- legacy documents

async def test_legacy_missing_allowed_emails_reads_as_empty(store):
    seed(store, domains=["example.org"])
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == (set(), {"example.org"})
    assert entry.is_allowed("hanna@example.org") and not entry.is_allowed("clara@example.com")


async def test_legacy_missing_allowed_domains_reads_as_empty(store):
    seed(store, emails=["clara@example.com"])
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == ({"clara@example.com"}, set())
    assert entry.is_allowed("clara@example.com") and not entry.is_allowed("hanna@example.com")


async def test_legacy_config_with_neither_list_reads_as_empty(store):
    seed(store, updated_at="2026-01-01")
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == (set(), set())


@pytest.mark.parametrize("field", ["allowed_emails", "allowed_domains"])
async def test_legacy_null_list_raises(store, field):
    seed(store, emails=["clara@example.com"], domains=["example.org"])
    store.docs["config"][field] = None
    with pytest.raises(TypeError):
        await store.repo.get_whitelist()


async def test_entries_are_returned_lowercase_and_stripped(store):
    seed(store, emails=["Clara@Example.com "], domains=[" Example.ORG"])
    entry = await store.repo.get_whitelist()
    assert entry.allowed_emails == {"clara@example.com"} and entry.allowed_domains == {"example.org"}
    assert entry.is_allowed("clara@example.com") and entry.is_allowed("hanna@example.org")


# --------------------------------------------------------------------------------------------------- errors

async def test_a_backend_error_propagates(store, monkeypatch):
    seed(store, emails=["clara@example.com"], domains=[])

    class Down:
        async def get(self, *a, **kw):
            raise RuntimeError("backend down")

    monkeypatch.setattr(store.repo.collection, "document", lambda _doc_id: Down())
    with pytest.raises(RuntimeError, match="backend down"):
        await store.repo.get_whitelist()
