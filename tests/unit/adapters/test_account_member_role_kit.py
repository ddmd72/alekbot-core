"""AccountRepository.set_member_role (follow-up of the P11 · G1 account kit): one member's entry of iam_policy, written
field by field, on the evaluating Firestore fake with the index refusal ON. Assertions are on the raw stored documents.

The defect it closes: a team invite added its member by rewriting the whole iam_policy map read a moment earlier
(`update_account`), so two invites accepted into one account at once kept only the member written last. The last test
runs the real `InviteCodeService` over the real repositories, as the invite kit does.
"""
import asyncio

import pytest
from google.api_core.exceptions import NotFound

import src.adapters.firestore_account_repo as account_module
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from tests.support.clock_instants import pin_datetime
from tests.unit.adapters.test_account_store_kit import ACCOUNTS, INDEXES_FILE, T0, Accounts
from tests.unit.adapters.test_invite_code_store_kit import ACCOUNTS as INVITE_ACCOUNTS
from tests.unit.adapters.test_invite_code_store_kit import OWNER, OWNER_ACC, InviteStore, invite


class Members(Accounts):
    """The account kit's world (its `seed` and raw `docs`), its real repository built here over `kit_db`."""

    def __init__(self, kit_db) -> None:
        self.db = kit_db(indexes=INDEXES_FILE, groups={ACCOUNTS: "development_domain_accounts_v2"})
        self.repo = FirestoreAccountRepository(self.db, ACCOUNTS)


@pytest.fixture
def accounts(monkeypatch, kit_db):
    pin_datetime(monkeypatch, account_module, T0)
    return Members(kit_db)


@pytest.fixture
def store(kit_db):
    return InviteStore(kit_db)


async def test_set_member_role_adds_one_entry_and_keeps_every_other_field(accounts):
    await accounts.seed()
    before = dict(accounts.docs["a1"])
    await accounts.repo.set_member_role("a1", "u2", "member")
    after = accounts.docs["a1"]
    assert after["iam_policy"] == {"u1": "owner", "u2": "member"}
    assert {k: v for k, v in after.items() if k != "iam_policy"} == {k: v for k, v in before.items() if k != "iam_policy"}


async def test_set_member_role_replaces_the_role_of_an_existing_member(accounts):
    await accounts.seed()
    await accounts.repo.set_member_role("a1", "u1", "viewer")
    assert accounts.docs["a1"]["iam_policy"] == {"u1": "viewer"}


async def test_set_member_role_repeated_changes_nothing(accounts):
    await accounts.seed()
    await accounts.repo.set_member_role("a1", "u2", "member")
    once = dict(accounts.docs["a1"])
    await accounts.repo.set_member_role("a1", "u2", "member")
    assert accounts.docs["a1"]["iam_policy"] == once["iam_policy"] == {"u1": "owner", "u2": "member"}


async def test_set_member_role_on_an_absent_account_raises_and_writes_nothing(accounts):
    with pytest.raises(NotFound):
        await accounts.repo.set_member_role("a1", "u2", "member")
    assert accounts.docs == {}


async def test_concurrent_set_member_role_calls_for_one_account_all_land(accounts):
    await accounts.seed()
    accounts.db.collection(ACCOUNTS).yield_on_get = True
    await asyncio.gather(*(accounts.repo.set_member_role("a1", f"u{n}", "member") for n in range(2, 6)))
    assert accounts.docs["a1"]["iam_policy"] == {"u1": "owner", **{f"u{n}": "member" for n in range(2, 6)}}


async def test_two_invites_accepted_into_one_account_at_once_both_become_members(store):
    """Flips when the accept writes the whole iam_policy it read (both accepts read {owner}, the second write drops the
    first member)."""
    await store.team("usr-a", "usr-b")
    await store.repo.create(invite("AAA-AAA-AAA"))
    await store.repo.create(invite("BBB-BBB-BBB"))
    store.db.collection(INVITE_ACCOUNTS).yield_on_get = True      # both accepts read the account before either writes
    results = await asyncio.gather(store.service.consume_team_invite("AAA-AAA-AAA", "usr-a"),
                                   store.service.consume_team_invite("BBB-BBB-BBB", "usr-b"), return_exceptions=True)
    assert results == [None, None]
    assert (await store.accounts.get_account(OWNER_ACC)).iam_policy == {OWNER: "OWNER", "usr-a": "member",
                                                                        "usr-b": "member"}
    assert (await store.account_of("usr-a"), await store.account_of("usr-b")) == (OWNER_ACC, OWNER_ACC)


async def test_a_member_id_with_a_dot_or_a_backtick_stays_one_key_of_the_map(accounts):
    """Flips when the field path is built unescaped (`iam_policy.u.2` nests a map `u` with a key `2`)."""
    await accounts.seed()
    await accounts.repo.set_member_role("a1", "u.2", "member")
    await accounts.repo.set_member_role("a1", "u`3", "viewer")
    assert accounts.docs["a1"]["iam_policy"] == {"u1": "owner", "u.2": "member", "u`3": "viewer"}
