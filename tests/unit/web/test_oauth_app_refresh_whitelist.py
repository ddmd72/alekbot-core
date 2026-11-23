"""`/auth/refresh` re-checks the stored user's email against the access whitelist, with the check sign-in uses
(owner decision 2026-11-18; `decisions/cabinet_oauth_whitelist_gate.md`).

A user removed from the list used to keep renewing their Cabinet session for as long as they refreshed; now the next
renewal is refused as any refused renewal is: 401 "Session no longer valid" and no new access token. The world is the
the world below builds (the real blueprint, device-session kit and repositories on the evaluating fake; Google a
tripwire), with the whitelist document seeded. A renewal presents the cookies of a live device session of `user-a`;
a refused renewal clears the cookies, so "no access token" is an access cookie with no value.
"""
from types import SimpleNamespace

import pytest
from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.composition.connector_registry import ConnectorRegistry
from src.domain.billing import AccountTier, BillingAccount
from src.domain.exceptions import AccessDeniedError
from src.domain.user import UserProfile
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from src.web.cabinet_auth import ACCESS_COOKIE, REFRESH_COOKIE
from src.web.oauth_app import create_oauth_blueprint
from tests.support.cabinet_session_kit import CabinetSessionKit
from tests.support.firestore_eval import EvalDb
from tests.unit.services.test_authentication_service_resume_session import World as ServiceWorld
from tests.unit.web.gate_fakes import Tripwire

CLARA = "clara@example.com"
ENV = SimpleNamespace(domain_users_collection="users", domain_whitelist_collection="whitelist", is_production=False)


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.accounts = FirestoreAccountRepository(self.db, "accounts")
        self.users = FirestoreUserRepository(self.db, ENV, self.accounts)
        registry = AuthProviderRegistry({"google": Tripwire("google", [])}, default_provider_name="google")
        auth = AuthenticationService(registry, self.users, self.accounts,
                                     whitelist_repo=FirestoreWhitelistRepository(self.db, ENV))
        self.kit = CabinetSessionKit()
        app = Quart("oauth_refresh_whitelist")
        app.register_blueprint(create_oauth_blueprint(
            auth_service=auth, cabinet_sessions=self.kit.verifier, device_sessions=self.kit.device_sessions,
            auth_registry=registry, auth_config=SimpleNamespace(oauth_redirect_uri="https://app.example/auth/callback"),
            connect_service=Tripwire("connect_service", []), connectors=ConnectorRegistry()))
        self.client = app.test_client()

    async def seed(self, user_account: str = "account-a", **user_fields) -> None:
        for acc, tier, role in (("account-a", AccountTier.FREE, "member"), ("account-b", AccountTier.PRO, "owner")):
            await self.accounts.create_account(BillingAccount(account_id=acc, tier=tier, iam_policy={"user-a": role}))
        await self.db.collection("users").document("user-a").set(
            UserProfile(user_id="user-a", account_id=user_account, **user_fields).model_dump())

    async def refresh(self):
        """`/auth/refresh` with the cookies of a live device session of `user-a`."""
        signed_in = self.kit.sign_in_now("user-a", "account-a")
        self.client.set_cookie("localhost", ACCESS_COOKIE, signed_in.access_token)
        self.client.set_cookie("localhost", REFRESH_COOKIE, signed_in.refresh_secret)
        return await self.client.post("/auth/refresh")


def _sets_access_cookie(resp) -> bool:
    return any(h.startswith(f"{ACCESS_COOKIE}=") and not h.startswith(f"{ACCESS_COOKIE}=;")
               for h in resp.headers.get_all("Set-Cookie"))


def allow(w, emails=(), domains=()) -> None:
    w.db.collection("whitelist").docs["config"] = {"allowed_emails": list(emails), "allowed_domains": list(domains)}


async def test_a_whitelisted_user_is_renewed():
    w = World()
    await w.seed(email=CLARA)
    allow(w, emails=[CLARA])
    resp = await w.refresh()
    assert resp.status_code == 200
    assert _sets_access_cookie(resp)


async def test_a_user_removed_from_the_whitelist_gets_401_and_no_access_token():
    """Flips when the renewal skips the whitelist."""
    w = World()
    await w.seed(email=CLARA)
    allow(w, emails=["hanna@example.org"])
    resp = await w.refresh()
    assert resp.status_code == 401
    assert (await resp.get_json()) == {"error": "Session no longer valid"}
    assert not _sets_access_cookie(resp)


async def test_a_whitelisted_domain_renews_and_case_does_not_matter():
    w = World()
    await w.seed(email="Clara@Example.COM")
    allow(w, domains=["example.com"])
    resp = await w.refresh()
    assert resp.status_code == 200


async def test_a_user_without_an_email_gets_401():
    w = World()
    await w.seed(email=None)
    allow(w, domains=["example.com"])
    resp = await w.refresh()
    assert resp.status_code == 401
    assert not _sets_access_cookie(resp)


async def test_an_absent_whitelist_refuses_every_renewal():
    """The repository's secure default (no list: nobody allowed) holds for renewals as it does for sign-in."""
    w = World()
    await w.seed(email=CLARA)
    resp = await w.refresh()
    assert resp.status_code == 401


async def test_resume_session_raises_access_denied_for_an_email_off_the_list():
    w = ServiceWorld()
    await w.seed(email=CLARA)
    allow(w, emails=["robin@example.net"])
    with pytest.raises(AccessDeniedError):
        await w.service.resume_session("user-a")


async def test_resume_session_returns_the_user_of_an_email_on_the_list():
    w = ServiceWorld()
    await w.seed(email=CLARA)
    allow(w, emails=[CLARA])
    user, account = await w.service.resume_session("user-a")
    assert (user.email, account.account_id) == (CLARA, "account-a")


async def test_a_whitelisted_user_whose_account_is_absent_is_still_refused():
    """The list is checked before the account is read; passing it does not skip the account check."""
    w = ServiceWorld()
    await w.seed(account=False, email=CLARA)
    allow(w, emails=[CLARA])
    with pytest.raises(AccessDeniedError, match="no stored account"):
        await w.service.resume_session("user-a")


async def test_an_empty_stored_email_renews_by_the_sign_in_email_on_the_list():
    """`user.email` is empty for some users; the email the last sign-in passed the gate with is the one checked."""
    w = World()
    await w.seed(email=None, auth_metadata={"email": CLARA, "email_verified": True})
    allow(w, emails=[CLARA])
    resp = await w.refresh()
    assert resp.status_code == 200
    assert _sets_access_cookie(resp)


async def test_a_sign_in_email_removed_from_the_list_gets_401_with_an_empty_stored_email():
    w = World()
    await w.seed(email=None, auth_metadata={"email": CLARA, "email_verified": True})
    allow(w, emails=["hanna@example.org"])
    resp = await w.refresh()
    assert resp.status_code == 401
    assert not _sets_access_cookie(resp)


async def test_the_sign_in_email_wins_over_a_stale_stored_email():
    """The stored email is still listed, the email the user now signs in with is not: the renewal is refused."""
    w = World()
    await w.seed(email="hanna@example.org", auth_metadata={"email": CLARA})
    allow(w, emails=["hanna@example.org"])
    resp = await w.refresh()
    assert resp.status_code == 401


async def test_a_sign_in_record_without_an_email_falls_back_to_the_stored_email():
    w = World()
    await w.seed(email=CLARA, auth_metadata={"name": "Clara"})
    allow(w, emails=[CLARA])
    resp = await w.refresh()
    assert resp.status_code == 200
