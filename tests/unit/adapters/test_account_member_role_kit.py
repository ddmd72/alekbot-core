"""AccountRepository.set_member_role (follow-up of the P11 · G1 account kit): one member's entry of iam_policy, written
field by field, on the evaluating Firestore fake with the index refusal ON. Assertions are on the raw stored documents.

The defect it closes: a team invite added its member by rewriting the whole iam_policy map read a moment earlier
(`update_account`), so two invites accepted into one account at once kept only the member written last. The last test
runs the real `InviteCodeService` over the real repositories, as the invite kit does.
"""
import asyncio

import pytest
from google.api_core.exceptions import NotFound

from tests.unit.adapters.test_account_store_kit import ACCOUNTS, accounts  # noqa: F401 (fixture)
from tests.unit.adapters.test_invite_code_store_kit import ACCOUNTS as INVITE_ACCOUNTS
from tests.unit.adapters.test_invite_code_store_kit import OWNER, OWNER_ACC, store, invite  # noqa: F401 (fixture)


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
