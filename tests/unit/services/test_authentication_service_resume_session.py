"""`AuthenticationService.resume_session`: the stored user and account a session renewal mints its token from
(P19-D1/D1b).

Real service over the real user and account repositories on the evaluating Firestore fake; Google is a tripwire (never
reached); a read outage is scripted at the repository it hits."""
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.domain.billing import AccountTier, BillingAccount
from src.domain.exceptions import AccessDeniedError
from src.domain.user import UserProfile
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import Tripwire

ENV = SimpleNamespace(domain_users_collection="users", domain_whitelist_collection="whitelist", is_production=False)
OWNER = "owner@example.com"


class UsersDuringAReadOutage(FirestoreUserRepository):
    async def get_user(self, user_id: str):
        raise ServiceUnavailable("firestore unavailable")


class World:
    def __init__(self, users_cls=FirestoreUserRepository) -> None:
        self.db = EvalDb()
        self.accounts = FirestoreAccountRepository(self.db, "accounts")
        self.users = users_cls(self.db, ENV, self.accounts)
        registry = AuthProviderRegistry({"google": Tripwire("google", [])}, default_provider_name="google")
        self.service = AuthenticationService(registry, self.users, self.accounts,
                                             whitelist_repo=FirestoreWhitelistRepository(self.db, ENV))

    async def seed(self, account: bool = True, **user_fields) -> None:
        # The stored user's email is on the whitelist, which a renewal re-checks (owner decision 2026-11-18), so each
        # refusal below is reached by its own cause, not by the list.
        await self.db.collection("whitelist").document("config").set({"allowed_emails": [OWNER], "allowed_domains": []})
        if account:
            await self.accounts.create_account(
                BillingAccount(account_id="account-a", tier=AccountTier.PRO, iam_policy={"user-a": "owner"}))
        await self.db.collection("users").document("user-a").set(
            UserProfile(user_id="user-a", **{"account_id": "account-a", "email": OWNER, **user_fields}).model_dump())


async def test_it_returns_the_stored_user_and_their_stored_account():
    w = World()
    await w.seed(email="owner@example.com")
    user, account = await w.service.resume_session("user-a")
    assert (user.user_id, user.email) == ("user-a", "owner@example.com")
    assert (account.account_id, account.tier, account.iam_policy) == ("account-a", AccountTier.PRO, {"user-a": "owner"})


async def test_an_absent_user_is_refused():
    w = World()
    with pytest.raises(AccessDeniedError, match="absent or inactive"):
        await w.service.resume_session("user-a")


async def test_an_inactive_user_is_refused():
    w = World()
    await w.seed(is_active=False)
    with pytest.raises(AccessDeniedError, match="absent or inactive"):
        await w.service.resume_session("user-a")


async def test_a_user_without_an_account_id_is_refused():
    w = World()
    await w.seed(account_id=None)
    with pytest.raises(AccessDeniedError, match="no stored account"):
        await w.service.resume_session("user-a")


async def test_a_user_whose_account_is_absent_is_refused():
    w = World()
    await w.seed(account=False)
    with pytest.raises(AccessDeniedError, match="no stored account"):
        await w.service.resume_session("user-a")


async def test_a_store_outage_propagates_and_is_not_a_refusal():
    w = World(users_cls=UsersDuringAReadOutage)
    await w.seed()
    with pytest.raises(ServiceUnavailable):
        await w.service.resume_session("user-a")
