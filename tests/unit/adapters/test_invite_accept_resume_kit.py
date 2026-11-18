"""A team-invite accept that a failed write interrupted is finished by the same user's retry with the same code.

The accept claims the code, then writes the membership, then moves the user into the account. A failure between those
writes used to strand the user: the code was used, so the retry was refused, while the user had been moved without a
membership (or held neither). Now any single failure leaves a state the claimant's retry completes, and nobody else
can take the claimed code.

The real `InviteCodeService` over the real Firestore repositories on the evaluating fake, as the invite kit builds them;
a failed write is a repository method that raises once, before it writes.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.services.invite_code_service import InviteCodeService
from tests.support.redelivery_world import require
from tests.unit.adapters.test_invite_code_store_kit import (
    ACCOUNTS, DIGEST_KEY, INDEXES_FILE, INVITES, NOW, OWNER, OWNER_ACC, USERS, WHITELIST, InviteStore, invite, key)


class Accepts(InviteStore):
    """The invite kit's world (`team`, `account_of`, raw `docs`), its real repositories and service built here over
    `kit_db`."""

    def __init__(self, kit_db) -> None:
        self.db = kit_db(indexes=INDEXES_FILE, groups={
            INVITES: "development_invite_codes_v2", USERS: "development_domain_users_v1",
            ACCOUNTS: "development_domain_accounts_v2", WHITELIST: "development_domain_whitelist_v1"})
        env = SimpleNamespace(domain_invite_codes_collection=INVITES, domain_users_collection=USERS,
                              domain_whitelist_collection=WHITELIST, is_production=False)
        self.repo = FirestoreInviteCodeRepository(self.db, env, digest_key=DIGEST_KEY)
        self.accounts = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.users = FirestoreUserRepository(self.db, env, self.accounts)
        self.service = InviteCodeService(self.repo, self.users, self.accounts, FirestoreWhitelistRepository(self.db, env))


@pytest.fixture
def store(kit_db):
    return Accepts(kit_db)


def fail_once(monkeypatch, repo, method: str) -> None:
    real = getattr(repo, method)
    calls = []

    async def failing(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("backend down")
        return await real(*args, **kwargs)

    monkeypatch.setattr(repo, method, failing)


async def members(store) -> dict:
    return (await store.accounts.get_account(OWNER_ACC)).iam_policy


async def test_a_failed_move_of_the_user_is_finished_by_the_retry(store, monkeypatch):
    await store.team("usr-new")
    await store.repo.create(invite(role="VIEWER"))
    fail_once(monkeypatch, store.users, "update_user")
    with pytest.raises(RuntimeError, match="backend down"):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert store.docs[key()]["used_by_user_id"] == "usr-new"
    assert await store.account_of("usr-new") == "acc-usr-new"          # not moved without its membership

    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert await store.account_of("usr-new") == OWNER_ACC
    assert await members(store) == {OWNER: "OWNER", "usr-new": "viewer"}


async def test_a_failed_membership_write_is_finished_by_the_retry(store, monkeypatch):
    await store.team("usr-new")
    await store.repo.create(invite())
    fail_once(monkeypatch, store.accounts, "set_member_role")
    with pytest.raises(RuntimeError, match="backend down"):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert await store.account_of("usr-new") == "acc-usr-new"
    assert "usr-new" not in await members(store)

    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert await store.account_of("usr-new") == OWNER_ACC
    assert await members(store) == {OWNER: "OWNER", "usr-new": "member"}


async def test_a_code_claimed_by_an_interrupted_accept_is_refused_to_anyone_else(store, monkeypatch):
    await store.team("usr-new", "usr-late")
    await store.repo.create(invite())
    fail_once(monkeypatch, store.users, "update_user")
    with pytest.raises(RuntimeError):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")

    with pytest.raises(ValueError, match="expired or already used"):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-late")
    assert await store.account_of("usr-late") == "acc-usr-late"
    assert "usr-late" not in await members(store)
    assert store.docs[key()]["used_by_user_id"] == "usr-new"


async def test_the_claimants_retry_finishes_even_after_the_code_expired(store, monkeypatch):
    """The claim was made while the code was valid: the retry completes that accept, it is not a new one."""
    await store.team("usr-new")
    await store.repo.create(invite())
    fail_once(monkeypatch, store.users, "update_user")
    with pytest.raises(RuntimeError):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    store.docs[key()] = {**store.docs[key()], "expires_at": NOW - timedelta(seconds=1)}

    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert await store.account_of("usr-new") == OWNER_ACC
    assert await members(store) == {OWNER: "OWNER", "usr-new": "member"}


async def test_a_completed_accept_retried_is_refused_as_a_used_code_and_changes_nothing(store):
    await store.team("usr-new")
    await store.repo.create(invite())
    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    before = (dict(store.docs[key()]), await members(store))
    with pytest.raises(ValueError, match="expired or already used"):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert (dict(store.docs[key()]), await members(store)) == before


async def test_a_completed_code_re_presented_after_the_user_moved_on_is_refused_and_changes_nothing(store):
    """Flips when the resume is told from the user's current account: after the accept the user moved to another
    account, and the old code would re-join the first one (past its expiry too)."""
    await store.team("usr-new")
    await store.repo.create(invite())
    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    user = await store.users.get_user("usr-new")
    user.account_id = "acc-usr-new"                                # moved on, e.g. by another team's invite
    await store.users.update_user(user)
    before = (dict(store.docs[key()]), await members(store))

    with pytest.raises(ValueError, match="expired or already used"):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert await store.account_of("usr-new") == "acc-usr-new"
    assert (dict(store.docs[key()]), await members(store)) == before


async def test_a_finished_accept_stamps_its_code_completed(store):
    await store.team("usr-new")
    await store.repo.create(invite())
    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    doc = store.docs[key()]
    assert doc["used_by_user_id"] == "usr-new"
    assert doc["used_at"] <= doc["completed_at"]


async def test_a_failed_completion_stamp_is_finished_by_the_retry(store, monkeypatch):
    await store.team("usr-new")
    await store.repo.create(invite())
    fail_once(monkeypatch, store.repo, "complete")
    with pytest.raises(RuntimeError, match="backend down"):
        await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    require(await store.account_of("usr-new") == OWNER_ACC, "the move landed before the stamp failed")
    assert "completed_at" not in store.docs[key()]

    await store.service.consume_team_invite("ABC-DEF-123", "usr-new")
    assert "completed_at" in store.docs[key()]
    assert await members(store) == {OWNER: "OWNER", "usr-new": "member"}
