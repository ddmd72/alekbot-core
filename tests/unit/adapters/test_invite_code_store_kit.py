"""InviteCodeRepository kit (P6 · T2): the docstring of `src/ports/invite_code_repository.py` checked against
`FirestoreInviteCodeRepository` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on the
raw stored documents and on what readers return.

Validity is the caller's: where the docstring says the caller refuses (an expired, used or unreadable invite), the real
`InviteCodeService` runs over the real Firestore user, account, whitelist and invite repositories on the same fake, so
the refusal and its absence of effects are checked on stored documents too.

Since S5 (SECRETS_AT_REST_RFC §2.5) the doc id is the keyed digest of the normalised code and no field holds the code:
`key(code)` addresses a stored document, a lookup returns the presented code normalised (`as_read`), a list carries only
the hint (`code` None). The digest itself and the absence of the code anywhere are `test_invite_code_digest_kit.py`'s.

`store` is the one fixture that selects the backend.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.domain.billing import BillingAccount
from src.domain.invite_code import InviteCode, InviteType, invite_hint, normalise_invite_code
from src.domain.secret_digest import keyed_digest
from src.domain.user import UserProfile
from src.services.invite_code_service import InviteCodeService
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
INVITES = "eval_invite_codes_v2"
USERS = "eval_domain_users_v1"
ACCOUNTS = "eval_domain_accounts_v2"
WHITELIST = "eval_domain_whitelist_v1"
OWNER, OWNER_ACC = "usr-owner", "acc-owner"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
LATER = NOW + timedelta(days=7)
DROP = object()
DIGEST_KEY = b"k" * 32


class InviteStore:
    """The real invite repository, and the real service with its real neighbours, over one EvalDb with the declared
    indexes, each collection mapped to its index group."""

    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={
            INVITES: "development_invite_codes_v2", USERS: "development_domain_users_v1",
            ACCOUNTS: "development_domain_accounts_v2", WHITELIST: "development_domain_whitelist_v1"})
        env = SimpleNamespace(domain_invite_codes_collection=INVITES, domain_users_collection=USERS,
                              domain_whitelist_collection=WHITELIST, is_production=False)
        self.repo = FirestoreInviteCodeRepository(self.db, env, digest_key=DIGEST_KEY)
        self.accounts = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.users = FirestoreUserRepository(self.db, env, self.accounts)
        self.service = InviteCodeService(self.repo, self.users, self.accounts, FirestoreWhitelistRepository(self.db, env))

    @property
    def docs(self) -> dict:
        return self.db.collection(INVITES).docs

    async def team(self, *members):
        """The owner's account, and one user per member id, each in an account of its own, all whitelisted."""
        await self.accounts.create_account(BillingAccount(account_id=OWNER_ACC, iam_policy={OWNER: "OWNER"}))
        for uid in members:
            await self.accounts.create_account(BillingAccount(account_id=f"acc-{uid}", iam_policy={uid: "OWNER"}))
            await self.users.update_user(UserProfile(user_id=uid, email=f"{uid}@example.com", account_id=f"acc-{uid}"))
        self.db.collection(WHITELIST).docs["config"] = {"allowed_emails": [f"{u}@example.com" for u in members],
                                                       "allowed_domains": []}

    async def account_of(self, uid):
        return (await self.users.get_user(uid)).account_id


@pytest.fixture
def store():
    return InviteStore()


def invite(code="ABC-DEF-123", **over) -> InviteCode:
    fields = dict(code=code, hint=invite_hint(code), user_id=OWNER, account_id=OWNER_ACC, type=InviteType.TEAM_INVITE,
                  expires_at=LATER, created_at=NOW)
    fields.update(over)
    return InviteCode(**fields)


def key(code="ABC-DEF-123") -> str:
    """The doc id the store keeps `code` under."""
    return keyed_digest(DIGEST_KEY, "invite", normalise_invite_code(code))


def as_read(inv: InviteCode) -> InviteCode:
    """`inv` as a lookup by its code returns it: every field as saved, the code normalised."""
    return replace(inv, code=normalise_invite_code(inv.code))


def raw(store, code="ABC-DEF-123", **over) -> str:
    """Seed the raw document of `invite(code)` under `key(code)` as the adapter writes it, with `over` on top of the
    stored fields (`DROP` removes a field)."""
    doc = {"hint": invite_hint(code), "user_id": OWNER, "account_id": OWNER_ACC, "type": "team_invite",
           "expires_at": LATER, "created_at": NOW, "role": "MEMBER"}
    for field, value in over.items():
        if value is DROP:
            doc.pop(field)
        else:
            doc[field] = value
    store.docs[key(code)] = doc
    return code


# --------------------------------------------------------------------------------------------------- create

async def test_create_stores_the_invite_under_the_digest_of_its_code_and_returns_it(store):
    inv = invite(role="VIEWER")
    assert await store.repo.create(inv) is inv
    assert store.docs == {key(): {
        "hint": "F123", "user_id": OWNER, "account_id": OWNER_ACC, "type": "team_invite",
        "expires_at": LATER, "created_at": NOW, "role": "VIEWER",
    }}


async def test_create_stores_platform_and_use_when_set(store):
    used = NOW + timedelta(hours=1)
    await store.repo.create(invite("SLF-LNK-001", type=InviteType.SELF_LINK, platform="slack", used_at=used,
                                   used_by_user_id="usr-2"))
    doc = store.docs[key("SLF-LNK-001")]
    assert (doc["type"], doc["platform"], doc["used_at"], doc["used_by_user_id"]) == ("self_link", "slack", used, "usr-2")


async def test_create_through_the_service_stores_a_new_unused_invite(store):
    inv = await store.service.generate_team_invite(OWNER, OWNER_ACC, "VIEWER")
    doc = store.docs[key(inv.code)]
    assert (doc["user_id"], doc["account_id"], doc["type"], doc["role"]) == (OWNER, OWNER_ACC, "team_invite", "VIEWER")
    assert "used_at" not in doc and "used_by_user_id" not in doc
    assert await store.repo.get_by_code(inv.code) == as_read(inv)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "known defect: create overwrites an existing code, so a collision rebinds the older invite to the new creator's "
    "account — src/adapters/firestore_invite_code_repo.py:83 (a 128-bit code since S5 makes a collision negligible; "
    "P6 T1 L2)"))
async def test_create_never_replaces_another_invite(store):
    await store.repo.create(invite())
    try:
        await store.repo.create(invite(user_id="usr-intruder", account_id="acc-intruder"))
    except Exception:       # refusing the second create is one way to keep the first; the docstring names no
        pass                # refusal type, so the fix PR picks it and narrows this
    assert (store.docs[key()]["user_id"], store.docs[key()]["account_id"]) == (OWNER, OWNER_ACC)


# ----------------------------------------------------------------------------------------------- get_by_code

async def test_get_returns_the_invite_as_saved_with_utc_datetimes(store):
    plus3 = timezone(timedelta(hours=3))
    saved = invite(type=InviteType.SELF_LINK, platform="telegram", role="VIEWER",
                   expires_at=datetime(2030, 5, 6, 10, 0, tzinfo=plus3), created_at=datetime(2030, 5, 1, 10, 0, tzinfo=plus3))
    await store.repo.create(saved)
    got = await store.repo.get_by_code("ABC-DEF-123")
    assert got == as_read(saved)
    assert got.type is InviteType.SELF_LINK
    for value in (got.expires_at, got.created_at):
        assert value.utcoffset() == timedelta(0)
    assert got.expires_at == datetime(2030, 5, 6, 7, 0, tzinfo=timezone.utc)


async def test_get_ignores_case_spaces_and_dashes_and_nothing_else(store):
    await store.repo.create(invite("ABC-DEF-123"))
    for typed in ("ABC-DEF-123", "abc-def-123", "ABC DEF 123", "ABCDEF123"):
        assert (await store.repo.get_by_code(typed)).code == "ABCDEF123", typed
    assert await store.repo.get_by_code("ABC-DEF-12") is None
    assert await store.repo.get_by_code("ABC-DEF-1234") is None
    assert await store.repo.get_by_code("ABC-DEF-124") is None


async def test_a_missing_invite_is_none_and_the_service_refuses_it(store):
    assert await store.repo.get_by_code("NOP-NOP-NOP") is None
    with pytest.raises(ValueError, match="Invalid invite code"):
        await store.service.validate_code("NOP-NOP-NOP")
    assert store.docs == {}


async def test_tenancy_none_the_holder_of_another_creators_code_gets_it(store):
    await store.repo.create(invite("AAA-AAA-AAA", user_id="usr-a", account_id="acc-a"))
    await store.repo.create(invite("BBB-BBB-BBB", user_id="usr-b", account_id="acc-b"))
    a, b = await store.repo.get_by_code("AAA-AAA-AAA"), await store.repo.get_by_code("BBB-BBB-BBB")
    assert (a.user_id, a.account_id, b.user_id, b.account_id) == ("usr-a", "acc-a", "usr-b", "acc-b")


async def test_validity_is_never_decided_here_used_and_expired_invites_are_returned_as_saved(store):
    expired_at = NOW - timedelta(seconds=1)
    used_at = NOW - timedelta(hours=2)
    await store.repo.create(invite("EXP-EXP-EXP", expires_at=expired_at))
    await store.repo.create(invite("USD-USD-USD", used_at=used_at, used_by_user_id="usr-2"))
    expired, used = await store.repo.get_by_code("EXP-EXP-EXP"), await store.repo.get_by_code("USD-USD-USD")
    assert (expired.expires_at, expired.used_at) == (expired_at, None)
    assert (used.used_at, used.used_by_user_id) == (used_at, "usr-2")
    assert expired.expires_at.utcoffset() == used.used_at.utcoffset() == timedelta(0)
    assert not expired.is_valid() and not used.is_valid()


@pytest.mark.parametrize("over", [dict(expires_at=NOW - timedelta(seconds=1)),
                                  dict(used_at=NOW, used_by_user_id="usr-2")], ids=["expired", "used"])
async def test_the_service_refuses_an_expired_or_used_invite_and_nothing_changes(store, over):
    await store.team("usr-new")
    await store.repo.create(invite(**over))
    before = {k: dict(v) for k, v in store.docs.items()}
    with pytest.raises(ValueError, match="expired or already used"):
        await store.service.validate_code("ABC-DEF-123")
    with pytest.raises(ValueError, match="expired or already used"):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert store.docs == before
    assert await store.account_of("usr-new") == "acc-usr-new"
    assert "usr-new" not in (await store.accounts.get_account(OWNER_ACC)).iam_policy


# ---------------------------------------------------------------------------------------------- legacy documents

async def test_legacy_missing_role_reads_member(store):
    raw(store, role=DROP)
    assert (await store.repo.get_by_code("ABC-DEF-123")).role == "MEMBER"


async def test_legacy_missing_optional_fields_read_none(store):
    raw(store)
    got = await store.repo.get_by_code("ABC-DEF-123")
    assert (got.platform, got.used_at, got.used_by_user_id) == (None, None, None)


@pytest.mark.parametrize("over,error", [
    (dict(type=DROP), KeyError), (dict(type="admin_invite"), ValueError), (dict(type=None), ValueError),
    (dict(user_id=DROP), KeyError), (dict(account_id=DROP), KeyError),
    (dict(expires_at=DROP), KeyError), (dict(created_at=DROP), KeyError),
], ids=["no-type", "unknown-type", "null-type", "no-user", "no-account", "no-expiry", "no-created"])
async def test_legacy_missing_or_unknown_type_or_another_missing_field_raises(store, over, error):
    raw(store, **over)
    with pytest.raises(error):
        await store.repo.get_by_code("ABC-DEF-123")


async def test_an_unreadable_invite_is_refused_and_nothing_changes(store):
    await store.team("usr-new")
    raw(store, type=DROP)
    before = {k: dict(v) for k, v in store.docs.items()}
    with pytest.raises(KeyError):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert store.docs == before
    assert await store.account_of("usr-new") == "acc-usr-new"


# ---------------------------------------------------------------------------------------------------- update

async def test_consuming_marks_the_stored_invite_used_and_a_second_use_is_refused(store):
    await store.team("usr-new", "usr-late")
    await store.repo.create(invite(role="VIEWER"))
    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    doc = store.docs[key()]
    assert doc["used_by_user_id"] == "usr-new"
    assert NOW <= doc["used_at"] <= datetime.now(timezone.utc)
    assert await store.account_of("usr-new") == OWNER_ACC
    assert "usr-new" in (await store.accounts.get_account(OWNER_ACC)).iam_policy
    with pytest.raises(ValueError, match="expired or already used"):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-late")
    assert await store.account_of("usr-late") == "acc-usr-late"


async def test_a_consumed_invite_grants_its_role_in_lowercase(store):
    await store.team("usr-new")
    await store.repo.create(invite(role="VIEWER"))
    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert (await store.accounts.get_account(OWNER_ACC)).iam_policy["usr-new"] == "viewer"


async def test_update_replaces_the_stored_invite_and_returns_it(store):
    await store.repo.create(invite(type=InviteType.SELF_LINK, platform="slack"))
    used_at = NOW + timedelta(minutes=5)
    new = invite(used_at=used_at, used_by_user_id="usr-2")       # no platform any more: the stored one goes
    assert await store.repo.update(new) is new
    assert store.docs[key()] == {
        "hint": "F123", "user_id": OWNER, "account_id": OWNER_ACC, "type": "team_invite",
        "expires_at": LATER, "created_at": NOW, "role": "MEMBER", "used_at": used_at, "used_by_user_id": "usr-2",
    }
    assert await store.repo.get_by_code("ABC-DEF-123") == as_read(new)


async def test_update_is_a_blind_overwrite_last_writer_wins(store):
    await store.repo.create(invite())
    await store.repo.update(invite(used_at=NOW, used_by_user_id="usr-1"))
    await store.repo.update(invite(role="VIEWER"))
    doc = store.docs[key()]
    assert doc["role"] == "VIEWER" and "used_at" not in doc and "used_by_user_id" not in doc


async def test_update_of_an_absent_code_creates_it(store):
    await store.repo.update(invite("NEW-NEW-NEW"))
    assert (await store.repo.get_by_code("NEW-NEW-NEW")) == as_read(invite("NEW-NEW-NEW"))


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "known defect: consume_team_invite reads, checks is_valid and writes with no precondition, so two concurrent "
    "consumers of one code both join — src/services/invite_code_service.py:86-152, "
    "src/adapters/firestore_invite_code_repo.py:89 (P6 T1 H3)"))
async def test_of_two_concurrent_consumers_of_one_code_exactly_one_joins(store):
    await store.team("usr-a", "usr-b")
    await store.repo.create(invite())
    store.db.collection(INVITES).yield_on_get = True      # both consumers read the invite before either writes
    results = await asyncio.gather(store.service.consume_team_invite("ABC-DEF-123", "usr-a"),
                                   store.service.consume_team_invite("ABC-DEF-123", "usr-b"), return_exceptions=True)
    joined = [uid for uid in ("usr-a", "usr-b") if await store.account_of(uid) == OWNER_ACC]
    assert len(joined) == 1, f"joined: {joined}"
    assert sum(isinstance(r, Exception) for r in results) == 1
    assert store.docs[key()]["used_by_user_id"] == joined[0]


# ---------------------------------------------------------------------------------------------- list_by_user

async def test_list_returns_every_invite_of_the_creator_used_and_expired_included(store):
    mine = []
    for i in range(30):
        over = {}
        if i % 3 == 1:
            over = dict(used_at=NOW, used_by_user_id=f"usr-{i}")
        elif i % 3 == 2:
            over = dict(expires_at=NOW - timedelta(days=1))
        inv = invite(f"AAA-MIN-{i:03d}", **over)      # hints N000..N029, one per invite
        mine.append(inv)
        await store.repo.create(inv)
        await store.repo.create(invite(f"OTH-{i:03d}-BBB", user_id="usr-other", account_id="acc-other"))
    listed = await store.repo.list_by_user(OWNER)
    assert len(listed) == 30
    assert sorted(listed, key=lambda x: x.hint) == sorted((replace(m, code=None) for m in mine), key=lambda x: x.hint)


async def test_list_never_returns_another_creators_invites(store):
    await store.repo.create(invite("OTH-OTH-OTH", user_id="usr-other", account_id=OWNER_ACC))
    assert await store.repo.list_by_user(OWNER) == []
    assert [i.hint for i in await store.repo.list_by_user("usr-other")] == [invite_hint("OTH-OTH-OTH")]
    assert await store.repo.list_by_user("usr-nobody") == []


async def test_list_raises_on_a_legacy_invite_without_type(store):
    raw(store, "LEG-LEG-LEG", type=DROP)
    await store.repo.create(invite())
    with pytest.raises(KeyError):
        await store.repo.list_by_user(OWNER)


async def test_list_reads_a_legacy_invite_without_role_as_member(store):
    raw(store, "LEG-LEG-LEG", role=DROP)
    assert [(i.code, i.hint, i.role) for i in await store.repo.list_by_user(OWNER)] == [(None, "GLEG", "MEMBER")]


# ---------------------------------------------------------------------------------------------------- errors

class _Down:
    async def get(self, *a, **kw):
        raise RuntimeError("backend down")

    async def set(self, *a, **kw):
        raise RuntimeError("backend down")


@pytest.mark.parametrize("call", [
    lambda repo: repo.get_by_code("ABC-DEF-123"),
    lambda repo: repo.create(invite("NEW-NEW-NEW")),
    lambda repo: repo.update(invite()),
])
async def test_a_backend_error_propagates(store, monkeypatch, call):
    await store.repo.create(invite())
    monkeypatch.setattr(store.repo.collection, "document", lambda _doc_id: _Down())
    with pytest.raises(RuntimeError, match="backend down"):
        await call(store.repo)


async def test_a_backend_error_in_list_propagates(store):
    await store.repo.create(invite())
    store.db.collection(INVITES).fail = RuntimeError("backend down")
    with pytest.raises(RuntimeError, match="backend down"):
        await store.repo.list_by_user(OWNER)
