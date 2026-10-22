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
    seed(store, emails=["ann@example.com", "bob@other.org"], domains=["company.com"])
    entry = await store.repo.get_whitelist()
    assert isinstance(entry, WhitelistEntry)
    assert entry.allowed_emails == {"ann@example.com", "bob@other.org"}
    assert entry.allowed_domains == {"company.com"}


async def test_an_allowed_email_and_an_email_of_an_allowed_domain_pass_and_nothing_else_does(store):
    seed(store, emails=["ann@example.com"], domains=["company.com"])
    entry = await store.repo.get_whitelist()
    assert entry.is_allowed("ann@example.com") and entry.is_allowed("Ann@Example.com ")
    assert entry.is_allowed("zoe@company.com")
    assert not entry.is_allowed("bob@example.com")
    assert not entry.is_allowed("ann@example.com.evil.org")
    assert not entry.is_allowed("eve@sub.company.com")


async def test_no_cache_a_removed_email_is_refused_on_the_next_call(store):
    seed(store, emails=["ann@example.com", "bob@example.com"], domains=[])
    assert (await store.repo.get_whitelist()).is_allowed("ann@example.com")
    seed(store, emails=["bob@example.com"], domains=[])            # the admin removes Ann
    after = await store.repo.get_whitelist()
    assert not after.is_allowed("ann@example.com")
    assert after.is_allowed("bob@example.com")


async def test_no_cache_a_removed_domain_is_refused_on_the_next_call(store):
    seed(store, emails=[], domains=["company.com"])
    assert (await store.repo.get_whitelist()).is_allowed("zoe@company.com")
    seed(store, emails=[], domains=[])
    assert not (await store.repo.get_whitelist()).is_allowed("zoe@company.com")


async def test_no_cache_an_added_email_passes_on_the_next_call(store):
    seed(store, emails=[], domains=[])
    assert not (await store.repo.get_whitelist()).is_allowed("ann@example.com")
    seed(store, emails=["ann@example.com"], domains=[])
    assert (await store.repo.get_whitelist()).is_allowed("ann@example.com")


async def test_the_read_does_not_change_the_stored_config(store):
    seed(store, emails=["ann@example.com"], domains=["company.com"], updated_at="kept")
    before = dict(store.docs["config"])
    await store.repo.get_whitelist()
    assert store.docs == {"config": before}


# ------------------------------------------------------------------------------------- missing config: no one

async def test_missing_config_is_an_empty_whitelist_and_no_one_is_allowed(store):
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == (set(), set())
    assert not entry.is_allowed("ann@example.com")
    assert store.docs == {}                                         # reading creates nothing


async def test_only_the_config_document_is_read(store):
    """Tenancy: one global whitelist, the singleton `config`; any other document in the collection grants nothing."""
    seed(store, emails=["ann@example.com"], domains=["company.com"], doc_id="acc-1")
    seed(store, emails=["eve@example.com"], domains=["evil.org"], doc_id="Config")
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == (set(), set())
    seed(store, emails=["bob@example.com"], domains=[])
    entry = await store.repo.get_whitelist()
    assert entry.allowed_emails == {"bob@example.com"} and entry.allowed_domains == set()


# ------------------------------------------------------------------------------------------- legacy documents

async def test_legacy_missing_allowed_emails_reads_as_empty(store):
    seed(store, domains=["company.com"])
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == (set(), {"company.com"})
    assert entry.is_allowed("zoe@company.com") and not entry.is_allowed("ann@example.com")


async def test_legacy_missing_allowed_domains_reads_as_empty(store):
    seed(store, emails=["ann@example.com"])
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == ({"ann@example.com"}, set())
    assert entry.is_allowed("ann@example.com") and not entry.is_allowed("zoe@example.com")


async def test_legacy_config_with_neither_list_reads_as_empty(store):
    seed(store, updated_at="2026-01-01")
    entry = await store.repo.get_whitelist()
    assert (entry.allowed_emails, entry.allowed_domains) == (set(), set())


@pytest.mark.parametrize("field", ["allowed_emails", "allowed_domains"])
async def test_legacy_null_list_raises(store, field):
    seed(store, emails=["ann@example.com"], domains=["company.com"])
    store.docs["config"][field] = None
    with pytest.raises(TypeError):
        await store.repo.get_whitelist()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "known defect: entries are returned as stored, so one written in mixed case outside the port never matches — "
    "src/adapters/firestore_whitelist_repo.py:90-91 (P6 T1 L1)"))
async def test_entries_are_returned_lowercase_and_stripped(store):
    seed(store, emails=["Ann@Example.com "], domains=[" Company.COM"])
    entry = await store.repo.get_whitelist()
    assert entry.allowed_emails == {"ann@example.com"} and entry.allowed_domains == {"company.com"}
    assert entry.is_allowed("ann@example.com") and entry.is_allowed("zoe@company.com")


# --------------------------------------------------------------------------------------------------- errors

async def test_a_backend_error_propagates(store, monkeypatch):
    seed(store, emails=["ann@example.com"], domains=[])

    class Down:
        async def get(self, *a, **kw):
            raise RuntimeError("backend down")

    monkeypatch.setattr(store.repo.collection, "document", lambda _doc_id: Down())
    with pytest.raises(RuntimeError, match="backend down"):
        await store.repo.get_whitelist()
