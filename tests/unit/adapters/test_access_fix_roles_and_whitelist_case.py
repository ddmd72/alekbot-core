"""Access fixes 2 and 3, on the same real repositories and the evaluating Firestore fake the P6 kits use.

2. A consumed team invite writes the lowercase Role value into iam_policy (every access check compares lowercase),
   for each role, and leaves the invite's own stored role as created.
3. Whitelist entries match case-insensitively: mixed-case entries stored outside the repository are returned lowercase
   and stripped, so the lowercased email that WhitelistEntry.is_allowed compares matches.
"""
import pytest

from src.ports.iam_port import Role
from tests.unit.adapters.test_invite_code_store_kit import OWNER, OWNER_ACC, InviteStore, invite
from tests.unit.adapters.test_whitelist_store_kit import WhitelistStore, seed


# ------------------------------------------------------------------------------------------------------ roles

@pytest.mark.parametrize("stored, expected", [("OWNER", Role.OWNER), ("MEMBER", Role.MEMBER), ("VIEWER", Role.VIEWER)])
async def test_a_consumed_invite_writes_the_role_value_the_checks_compare(stored, expected):
    store = InviteStore()
    await store.team("usr-new")
    await store.repo.create(invite(role=stored))
    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    account = await store.accounts.get_account(OWNER_ACC)
    assert account.iam_policy["usr-new"] == expected.value
    assert Role(account.iam_policy["usr-new"]) is expected          # a stored value readers resolve as a Role
    assert account.iam_policy[OWNER] == "OWNER"                       # the existing entries are not rewritten
    assert store.docs["ABC-DEF-123"]["role"] == stored                # the invite keeps its own role as created
    assert store.docs["ABC-DEF-123"]["used_by_user_id"] == "usr-new"


async def test_an_invited_owner_is_resolved_as_an_owner_by_the_role_checks():
    store = InviteStore()
    await store.team("usr-new")
    await store.repo.create(invite(role="OWNER"))
    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    stored = (await store.accounts.get_account(OWNER_ACC)).iam_policy["usr-new"]
    assert Role(stored) is Role.OWNER


async def test_an_invite_with_an_unknown_role_is_refused_before_anything_is_written():
    store = InviteStore()
    await store.team("usr-new")
    await store.repo.create(invite(role="ROOT"))
    with pytest.raises(ValueError):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert await store.account_of("usr-new") == "acc-usr-new"
    assert "usr-new" not in (await store.accounts.get_account(OWNER_ACC)).iam_policy
    assert store.docs["ABC-DEF-123"].get("used_by_user_id") is None


# -------------------------------------------------------------------------------------------------- whitelist

async def test_a_mixed_case_stored_email_matches_the_lowercased_email():
    store = WhitelistStore()
    seed(store, emails=["Ann@Example.COM"], domains=[])
    entry = await store.repo.get_whitelist()
    assert entry.allowed_emails == {"ann@example.com"}
    assert entry.is_allowed("ann@example.com")


async def test_a_mixed_case_stored_domain_matches_the_lowercased_email():
    store = WhitelistStore()
    seed(store, emails=[], domains=["Company.COM"])
    entry = await store.repo.get_whitelist()
    assert entry.allowed_domains == {"company.com"}
    assert entry.is_allowed("zoe@company.com") and not entry.is_allowed("zoe@other.com")


async def test_whitespace_around_entries_is_stripped_and_blank_ones_are_dropped():
    store = WhitelistStore()
    seed(store, emails=["  ann@example.com\n", "   "], domains=["\tcompany.com ", ""])
    entry = await store.repo.get_whitelist()
    assert entry.allowed_emails == {"ann@example.com"}
    assert entry.allowed_domains == {"company.com"}
    assert not entry.is_allowed("zoe@")                               # a blank domain entry would have let this in


async def test_the_stored_document_is_not_rewritten_by_a_read():
    store = WhitelistStore()
    seed(store, emails=["Ann@Example.com "], domains=[" Company.COM"])
    await store.repo.get_whitelist()
    assert store.docs["config"] == {"allowed_emails": ["Ann@Example.com "], "allowed_domains": [" Company.COM"]}
