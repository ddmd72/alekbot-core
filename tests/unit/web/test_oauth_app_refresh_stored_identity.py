"""`/auth/refresh` mints the new access token from the stored user and account, never from the refresh token's payload
(P19-D1/D1b; `src/web/oauth_app.py`, `AuthenticationService.resume_session`).

Real session service, real blueprint, real user/account/whitelist repositories on the evaluating Firestore fake; Google
is a tripwire, never reached (a refresh does not sign in). A read outage is scripted at the repository it hits."""
from types import SimpleNamespace

from google.api_core.exceptions import ServiceUnavailable
from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.composition.connector_registry import ConnectorRegistry
from src.domain.billing import AccountTier, BillingAccount
from src.domain.user import UserProfile
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from src.web.oauth_app import create_oauth_blueprint
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import Tripwire, refresh_token, session_service

ENV = SimpleNamespace(domain_users_collection="users", domain_whitelist_collection="whitelist", is_production=False)


class UsersDuringAReadOutage(FirestoreUserRepository):
    async def get_user(self, user_id: str):
        raise ServiceUnavailable("firestore unavailable")


class World:
    def __init__(self, users_cls=FirestoreUserRepository) -> None:
        self.db = EvalDb()
        self.accounts = FirestoreAccountRepository(self.db, "accounts")
        self.users = users_cls(self.db, ENV, self.accounts)
        registry = AuthProviderRegistry({"google": Tripwire("google", [])}, default_provider_name="google")
        auth = AuthenticationService(registry, self.users, self.accounts,
                                     whitelist_repo=FirestoreWhitelistRepository(self.db, ENV))
        app = Quart("oauth_refresh_identity")
        app.register_blueprint(create_oauth_blueprint(
            auth_service=auth, session_service=session_service(), auth_registry=registry,
            auth_config=SimpleNamespace(oauth_redirect_uri="https://app.example/auth/callback",
                                        access_token_ttl=86400, refresh_token_ttl=86400),
            connect_service=Tripwire("connect_service", []), connectors=ConnectorRegistry()))
        self.client = app.test_client()

    async def seed(self, user_account: str = "account-a", **user_fields) -> None:
        for acc, tier, role in (("account-a", AccountTier.FREE, "member"), ("account-b", AccountTier.PRO, "owner")):
            await self.accounts.create_account(BillingAccount(account_id=acc, tier=tier, iam_policy={"user-a": role}))
        await self.db.collection("users").document("user-a").set(
            UserProfile(user_id="user-a", account_id=user_account, **user_fields).model_dump())

    async def refresh(self, token: str):
        self.client.set_cookie("localhost", "refresh_token", token)
        return await self.client.post("/auth/refresh")


def _sets_access_cookie(resp) -> bool:
    return any(h.startswith("access_token=") for h in resp.headers.get_all("Set-Cookie"))


async def test_the_new_token_carries_the_stored_accounts_tier_and_role():
    w = World()
    await w.seed(user_account="account-b")
    resp = await w.refresh(refresh_token("user-a", "account-b"))
    assert resp.status_code == 200
    claims = session_service().verify_access_token((await resp.get_json())["access_token"])
    assert (claims["account_id"], claims["tier"], claims["role"]) == ("account-b", "pro", "owner")


async def test_the_new_token_names_the_users_stored_account_not_the_one_in_the_refresh_token():
    # The user moved to account-b (a team invite) after the refresh token was minted for account-a: the renewal is
    # derived from what is stored, as a sign-in would be.
    w = World()
    await w.seed(user_account="account-b")
    resp = await w.refresh(refresh_token("user-a", "account-a"))
    assert resp.status_code == 200
    claims = session_service().verify_access_token((await resp.get_json())["access_token"])
    assert (claims["account_id"], claims["tier"]) == ("account-b", "pro")


async def test_an_inactive_user_gets_401_and_no_access_token():
    w = World()
    await w.seed(is_active=False)
    resp = await w.refresh(refresh_token("user-a", "account-a"))
    assert resp.status_code == 401
    assert (await resp.get_json()) == {"error": "Session no longer valid"}
    assert not _sets_access_cookie(resp)


async def test_a_store_outage_is_500_with_no_access_token_and_no_detail():
    w = World(users_cls=UsersDuringAReadOutage)
    await w.seed()
    resp = await w.refresh(refresh_token("user-a", "account-a"))
    assert resp.status_code == 500
    assert (await resp.get_json()) == {"error": "Token refresh failed"}
    assert not _sets_access_cookie(resp)
