"""P11-G2-4 through two real callers of a `UserRepository` profile write: the chat's language switch
(`LanguagePreferenceService.set_preference`) and a Cabinet sign-in refreshing `auth_metadata`
(`AuthenticationService.handle_oauth_callback`), both over the real `FirestoreUserRepository`, account, whitelist and
prompt-override repositories on one `EvalDb`. Google (the `AuthPort` boundary) is scripted.

Each caller reads the profile and changes its own fields. They used to write the whole profile back with no
precondition, so when both read before either wrote, the later write reverted the other's change; now each writes only
its fields (`update_user_fields`, owner decision O3) and both changes are kept (P11-G2-4 fixed). A `Rendezvous` right
after each caller's read forces that interleaving (a query never yields on `EvalDb`); a serial run fails `rv.met`.

The OAuth links (a sign-in's by-email attach and `link_oauth_identity`) are pinned the same way against a language
switch: each writes only `external_user_id` and `auth_metadata`.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.domain.auth import OAuthTokens, OAuthUserInfo, TokenClaims
from src.domain.billing import BillingAccount
from src.domain.language import LanguageCode
from src.domain.user import UserProfile
from src.ports.auth_port import AuthPort
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from src.services.language_preference_service import LanguagePreferenceService
from src.services.language_resolver import LanguageResolver
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import Rendezvous, require

EMAIL = "alex@example.com"
ENV = SimpleNamespace(domain_users_collection="users", domain_whitelist_collection="whitelist", is_production=False)


class Google(AuthPort):
    def get_provider_name(self) -> str:
        return "google"

    def get_authorization_url(self, state: str, redirect_uri: str) -> str:
        return "https://accounts.example/auth"

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> OAuthTokens:
        return OAuthTokens(access_token="g-at", id_token="g-id", expires_in=3600)

    async def verify_token(self, id_token: str) -> TokenClaims:
        now = datetime.now(timezone.utc)
        return TokenClaims(sub="sub-1", iss="https://accounts.google.com", aud="client", exp=now + timedelta(hours=1),
                           iat=now, email=EMAIL, email_verified=True, name="Alex Doe")

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        return OAuthUserInfo(sub="sub-1", email=EMAIL, email_verified=True, name="Alex Doe", locale="pt-PT")

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        accounts = FirestoreAccountRepository(self.db, "accounts")
        self.users = FirestoreUserRepository(self.db, ENV, accounts)
        self.language = LanguagePreferenceService(
            self.users, FirestoreAgentProfileRepository(self.db, "profiles", "overrides"),
            SimpleNamespace(invalidate_cache=lambda: None), LanguageResolver(self.users, accounts))
        self.auth = AuthenticationService(AuthProviderRegistry({"google": Google()}, default_provider_name="google"),
                                          self.users, accounts, whitelist_repo=FirestoreWhitelistRepository(self.db, ENV))
        self.accounts = accounts

    async def seed(self) -> None:
        await self.db.collection("whitelist").document("config").set({"allowed_emails": [EMAIL]})
        await self.accounts.create_account(BillingAccount(account_id="a1"))
        await self.users.create_user(UserProfile(user_id="u1", account_id="a1", email=EMAIL,
                                                 external_user_id="google|sub-1"))

    async def switch_language(self, rv=None) -> None:
        try:
            await self.language.set_preference("u1", LanguageCode.FR, agent_mirror=False)
        finally:
            if rv:
                rv.done()

    async def sign_in(self, rv=None) -> None:
        try:
            await self.auth.handle_oauth_callback(code="c", redirect_uri="https://app.example/cb")
        finally:
            if rv:
                rv.done()

    def stored(self) -> dict:
        return self.db.collection("users").docs["u1"]


async def test_a_language_switch_then_a_sign_in_keep_both_changes():
    w = World()
    await w.seed()
    await w.switch_language()
    await w.sign_in()
    assert w.stored()["config"]["preferred_language"] == "fr"
    assert w.stored()["auth_metadata"]["locale"] == "pt-PT"
    assert "LANG_FIXED_FR" in w.db.collection("overrides").docs["USER_u1"]["tokens"]


async def test_a_language_switch_during_a_sign_in_keeps_both_changes():
    """Holds because the callers write only the fields they changed (a field-level update, owner decision O3)."""
    w = World()
    await w.seed()
    rv = Rendezvous()
    rv.wrap(w.users, "get_user")                    # the language switch's read
    rv.wrap(w.users, "get_user_by_external_id")     # the sign-in's read
    await asyncio.gather(w.switch_language(rv), w.sign_in(rv))
    require(rv.met and not rv.stalled, "both callers read the profile before either wrote it")
    stored = w.stored()
    assert (stored["config"]["preferred_language"], (stored["auth_metadata"] or {}).get("locale")) == ("fr", "pt-PT")


async def _linking_by_email(w: World) -> None:
    await w.auth.handle_oauth_callback(code="c", redirect_uri="https://app.example/cb")


async def _linking_signed_in(w: World) -> None:
    await w.auth.link_oauth_identity("u1", code="c", redirect_uri="https://app.example/cb")


@pytest.mark.parametrize("link", [_linking_by_email, _linking_signed_in], ids=["by-email attach", "link_oauth_identity"])
async def test_an_oauth_link_during_a_language_switch_keeps_both_changes(link):
    """Both links write only `external_user_id` and `auth_metadata`, so the language a concurrent switch wrote stays.
    The link reads first; the switch reads and writes while the link waits, then the link writes: the order in which a
    whole-profile write reverted the switch."""
    w = World()
    await w.seed()
    await w.users.create_user(UserProfile(user_id="u1", account_id="a1", email=EMAIL, display_name="Alex Doe"))
    rv = Rendezvous()
    rv.wrap(w.users, "get_user")                    # the language switch's read and link_oauth_identity's
    rv.wrap(w.users, "get_user_by_email")           # the by-email attach's read

    async def switch() -> None:
        try:
            await rv.reached.wait()
            await w.language.set_preference("u1", LanguageCode.EN, agent_mirror=False)
        finally:
            rv.done()

    async def linking() -> None:
        try:
            await link(w)
        finally:
            rv.done()

    await asyncio.gather(linking(), switch())
    require(rv.met and not rv.stalled, "both callers read the profile before either wrote it")
    stored = w.stored()
    assert (stored["config"]["preferred_language"], stored["config"]["agent_mirror"]) == ("en", False)
    assert stored["external_user_id"] == "google|sub-1"
    assert (stored["auth_metadata"]["provider"], stored["auth_metadata"]["locale"]) == ("google", "pt-PT")
