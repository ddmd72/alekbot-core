"""AuthenticationService paths the sign-in tests do not reach (P7, C1: the file is held at 100 % lines).

Real service over the real user, account and whitelist repositories on the evaluating Firestore fake; Google (the
`AuthPort` boundary) is scripted, and a Firestore write outage is scripted at the repository it hits."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.domain.auth import OAuthTokens, OAuthUserInfo, TokenClaims
from src.domain.billing import BillingAccount
from src.domain.user import UserProfile
from src.ports.auth_port import AuthPort
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from tests.support.firestore_eval import EvalDb

EMAIL = "owner@example.com"
ENV = SimpleNamespace(domain_users_collection="users", domain_whitelist_collection="whitelist", is_production=False)


class Google(AuthPort):
    def __init__(self, user_info_fails: bool = False) -> None:
        self.user_info_fails = user_info_fails

    def get_provider_name(self) -> str:
        return "google"

    def get_authorization_url(self, state: str, redirect_uri: str) -> str:
        return "https://accounts.example/auth"

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> OAuthTokens:
        return OAuthTokens(access_token="g-at", id_token="g-id", expires_in=3600)

    async def verify_token(self, id_token: str) -> TokenClaims:
        now = datetime.now(timezone.utc)
        return TokenClaims(sub="sub-1", iss="https://accounts.google.com", aud="client", exp=now + timedelta(hours=1),
                           iat=now, email=EMAIL, email_verified=True, name="From Claims")

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        if self.user_info_fails:
            raise RuntimeError("userinfo endpoint 503")
        return OAuthUserInfo(sub="sub-1", email=EMAIL, email_verified=True, name="From Userinfo")

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError


class UsersDuringAWriteOutage(FirestoreUserRepository):
    """Reads work; the write the sign-in needs fails the way a Firestore outage does."""

    async def update_user(self, user: UserProfile) -> UserProfile:
        raise ServiceUnavailable("firestore unavailable")


class World:
    def __init__(self, provider: Google = None, users_cls=FirestoreUserRepository) -> None:
        self.db = EvalDb()
        self.accounts = FirestoreAccountRepository(self.db, "accounts")
        self.users = users_cls(self.db, ENV, self.accounts)
        self.service = AuthenticationService(
            AuthProviderRegistry({"google": provider or Google()}, default_provider_name="google"),
            self.users, self.accounts, whitelist_repo=FirestoreWhitelistRepository(self.db, ENV))

    async def seed(self, **user_fields) -> None:
        await self.db.collection("whitelist").document("config").set({"allowed_emails": [EMAIL]})
        await self.accounts.create_account(BillingAccount(account_id="account-a"))
        await self.db.collection("users").document("user-a").set(
            UserProfile(user_id="user-a", account_id="account-a", **user_fields).model_dump())


async def test_sign_in_that_cannot_store_the_link_by_email_fails_and_links_nothing():
    w = World(users_cls=UsersDuringAWriteOutage)
    await w.seed(email=EMAIL)
    with pytest.raises(ServiceUnavailable):
        await w.service.handle_oauth_callback(code="c", redirect_uri="https://app.example/cb")
    assert w.db.collection("users").docs["user-a"]["external_user_id"] is None


async def test_sign_in_whose_account_cannot_be_read_fails():
    w = World()
    await w.seed(external_user_id="google|sub-1")
    await w.db.collection("accounts").document("account-a").set({"account_id": "account-a", "tier": "no-such-tier"})
    with pytest.raises(ValueError):          # a pydantic ValidationError: the stored account is not a BillingAccount
        await w.service.handle_oauth_callback(code="c", redirect_uri="https://app.example/cb")


async def test_link_falls_back_to_the_verified_claims_when_userinfo_fails():
    w = World(Google(user_info_fails=True))
    await w.seed()
    user = await w.service.link_oauth_identity(user_id="user-a", code="c", redirect_uri="https://app.example/cb")
    stored = w.db.collection("users").docs["user-a"]
    assert user.external_user_id == stored["external_user_id"] == "google|sub-1"
    assert stored["auth_metadata"]["email"] == EMAIL and stored["auth_metadata"]["name"] == "From Claims"


async def test_lookups_by_external_and_platform_identity_read_the_stored_user():
    w = World()
    await w.seed(external_user_id="google|sub-1", platform_identities={"slack": "U1"})
    assert (await w.service.get_user_by_external_id("google|sub-1")).user_id == "user-a"
    assert (await w.service.get_user_by_platform_id("slack", "U1")).user_id == "user-a"
    assert await w.service.get_user_by_external_id("google|sub-2") is None
    assert await w.service.get_user_by_platform_id("telegram", "U1") is None
