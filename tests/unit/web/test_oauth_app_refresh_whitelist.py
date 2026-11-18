"""`/auth/refresh` re-checks the stored user's email against the access whitelist, with the check sign-in uses
(owner decision 2026-11-18; `decisions/cabinet_oauth_whitelist_gate.md`).

A user removed from the list used to keep renewing their Cabinet session for as long as they refreshed; now the next
renewal is refused as any refused renewal is: 401 "Session no longer valid" and no new access token. The world is the
one `test_oauth_app_refresh_stored_identity.py` builds (real session service, blueprint and repositories on the
evaluating fake; Google a tripwire), with the whitelist document seeded.
"""
import pytest

from src.domain.exceptions import AccessDeniedError
from tests.unit.services.test_authentication_service_resume_session import World as ServiceWorld
from tests.unit.web.gate_fakes import refresh_token
from tests.unit.web.test_oauth_app_refresh_stored_identity import World, _sets_access_cookie

CLARA = "clara@example.com"


def allow(w, emails=(), domains=()) -> None:
    w.db.collection("whitelist").docs["config"] = {"allowed_emails": list(emails), "allowed_domains": list(domains)}


async def test_a_whitelisted_user_is_renewed():
    w = World()
    await w.seed(email=CLARA)
    allow(w, emails=[CLARA])
    resp = await w.refresh(refresh_token("user-a", "account-a"))
    assert resp.status_code == 200
    assert _sets_access_cookie(resp)


async def test_a_user_removed_from_the_whitelist_gets_401_and_no_access_token():
    """Flips when the renewal skips the whitelist."""
    w = World()
    await w.seed(email=CLARA)
    allow(w, emails=["hanna@example.org"])
    resp = await w.refresh(refresh_token("user-a", "account-a"))
    assert resp.status_code == 401
    assert (await resp.get_json()) == {"error": "Session no longer valid"}
    assert not _sets_access_cookie(resp)


async def test_a_whitelisted_domain_renews_and_case_does_not_matter():
    w = World()
    await w.seed(email="Clara@Example.COM")
    allow(w, domains=["example.com"])
    resp = await w.refresh(refresh_token("user-a", "account-a"))
    assert resp.status_code == 200


async def test_a_user_without_an_email_gets_401():
    w = World()
    await w.seed(email=None)
    allow(w, domains=["example.com"])
    resp = await w.refresh(refresh_token("user-a", "account-a"))
    assert resp.status_code == 401
    assert not _sets_access_cookie(resp)


async def test_an_absent_whitelist_refuses_every_renewal():
    """The repository's secure default (no list: nobody allowed) holds for renewals as it does for sign-in."""
    w = World()
    await w.seed(email=CLARA)
    resp = await w.refresh(refresh_token("user-a", "account-a"))
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
