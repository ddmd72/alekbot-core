"""
Whitelist gate on Google OAuth sign-in (AuthenticationService).

Every OAuth sign-in — new user, existing user, link-by-email, explicit link —
must pass: email present in the verified ID token, email_verified is True, and
the email is whitelisted. A rejected identity never reaches the user repository.

Decision: docs/04_solution_strategy/decisions/cabinet_oauth_whitelist_gate.md
"""
from datetime import datetime
from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

from src.domain.billing import AccountTier, BillingAccount
from src.domain.exceptions import AccessDeniedError
from src.domain.user import UserProfile
from src.domain.whitelist import WhitelistEntry
from src.ports.auth_port import OAuthTokens, OAuthUserInfo, TokenClaims
from src.ports.whitelist_repository import WhitelistRepository
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService

ALLOWED = "owner@example.com"


def _claims(email: Optional[str], verified: Optional[bool]) -> TokenClaims:
    return TokenClaims(
        sub="sub-1",
        iss="https://accounts.google.com",
        aud="client",
        exp=datetime.now(),
        iat=datetime.now(),
        email=email,
        email_verified=verified,
    )


@pytest.fixture
def provider():
    p = Mock()
    p.get_provider_name.return_value = "firebase"
    p.exchange_code_for_tokens = AsyncMock(
        return_value=OAuthTokens(access_token="at", id_token="it", expires_in=3600)
    )
    p.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(sub="sub-1", email=ALLOWED, email_verified=True, name="Owner")
    )
    return p


@pytest.fixture
def user_repo():
    repo = Mock()
    repo.get_user_by_external_id = AsyncMock(return_value=None)
    repo.get_user_by_email = AsyncMock(return_value=None)
    repo.get_user = AsyncMock(return_value=None)
    repo.update_user = AsyncMock(side_effect=lambda u: u)
    repo.create_user = AsyncMock(side_effect=lambda u: u)
    return repo


@pytest.fixture
def account_repo():
    repo = Mock()
    repo.create_account = AsyncMock(side_effect=lambda a: a)
    repo.get_account = AsyncMock(
        side_effect=lambda account_id: BillingAccount(
            account_id=account_id, tier=AccountTier.FREE, iam_policy={}
        )
    )
    return repo


@pytest.fixture
def whitelist_repo():
    repo = AsyncMock(spec=WhitelistRepository)
    repo.get_whitelist.return_value = WhitelistEntry(
        allowed_emails={ALLOWED}, allowed_domains={"team.example"}
    )
    return repo


@pytest.fixture
def service(provider, user_repo, account_repo, whitelist_repo):
    registry = Mock(spec=AuthProviderRegistry)
    registry.get_provider.return_value = provider
    return AuthenticationService(
        registry, user_repo, account_repo, whitelist_repo=whitelist_repo
    )


def _assert_no_user_access(user_repo, account_repo):
    user_repo.get_user_by_external_id.assert_not_called()
    user_repo.get_user_by_email.assert_not_called()
    user_repo.get_user.assert_not_called()
    user_repo.update_user.assert_not_called()
    user_repo.create_user.assert_not_called()
    account_repo.create_account.assert_not_called()


# ---------------------------------------------------------------------------
# handle_oauth_callback
# ---------------------------------------------------------------------------


async def test_new_user_not_in_whitelist_is_rejected_before_any_user_access(
    service, provider, user_repo, account_repo
):
    provider.verify_token = AsyncMock(return_value=_claims("stranger@gmail.com", True))

    with pytest.raises(AccessDeniedError):
        await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    _assert_no_user_access(user_repo, account_repo)


async def test_whitelisted_new_user_is_registered(service, provider, user_repo, account_repo):
    provider.verify_token = AsyncMock(return_value=_claims(ALLOWED, True))

    user, account, _ = await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    user_repo.create_user.assert_awaited_once()
    account_repo.create_account.assert_awaited_once()
    assert user.external_user_id == "firebase|sub-1"
    assert account.account_id == user.account_id


async def test_whitelisted_domain_is_allowed(service, provider, user_repo):
    provider.verify_token = AsyncMock(return_value=_claims("someone@team.example", True))

    await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    user_repo.create_user.assert_awaited_once()


async def test_whitelist_match_is_case_insensitive(service, provider, user_repo):
    provider.verify_token = AsyncMock(return_value=_claims("Owner@Example.COM", True))

    await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    user_repo.create_user.assert_awaited_once()


async def test_existing_user_removed_from_whitelist_is_rejected(
    service, provider, user_repo, account_repo
):
    """Revocation: an existing user whose email left the whitelist cannot sign in."""
    user_repo.get_user_by_external_id = AsyncMock(
        return_value=UserProfile(
            user_id="u1", external_user_id="firebase|sub-1",
            email="revoked@gmail.com", account_id="a1",
        )
    )
    provider.verify_token = AsyncMock(return_value=_claims("revoked@gmail.com", True))

    with pytest.raises(AccessDeniedError):
        await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    _assert_no_user_access(user_repo, account_repo)


async def test_existing_whitelisted_user_signs_in(service, provider, user_repo):
    existing = UserProfile(
        user_id="u1", external_user_id="firebase|sub-1", email=ALLOWED, account_id="a1"
    )
    user_repo.get_user_by_external_id = AsyncMock(return_value=existing)
    provider.verify_token = AsyncMock(return_value=_claims(ALLOWED, True))

    user, account, _ = await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    assert user.user_id == "u1"
    assert account.account_id == "a1"
    user_repo.create_user.assert_not_called()


@pytest.mark.parametrize("verified", [False, None])
async def test_unverified_email_is_rejected(service, provider, user_repo, account_repo, verified):
    provider.verify_token = AsyncMock(return_value=_claims(ALLOWED, verified))

    with pytest.raises(AccessDeniedError):
        await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    _assert_no_user_access(user_repo, account_repo)


@pytest.mark.parametrize("email", [None, "", "   "])
async def test_missing_email_is_rejected(service, provider, user_repo, account_repo, email):
    """The old 'no email → register anyway' branch must not exist."""
    provider.verify_token = AsyncMock(return_value=_claims(email, True))

    with pytest.raises(AccessDeniedError):
        await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    _assert_no_user_access(user_repo, account_repo)


async def test_gate_reads_id_token_not_userinfo(service, provider, user_repo, account_repo):
    """userinfo claiming a whitelisted email cannot override the signed ID token."""
    provider.verify_token = AsyncMock(return_value=_claims("stranger@gmail.com", True))
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(sub="sub-1", email=ALLOWED, email_verified=True)
    )

    with pytest.raises(AccessDeniedError):
        await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    _assert_no_user_access(user_repo, account_repo)


async def test_empty_whitelist_denies_everyone(service, provider, whitelist_repo, user_repo):
    """Missing whitelist config = deny all (repo returns an empty entry)."""
    whitelist_repo.get_whitelist.return_value = WhitelistEntry(
        allowed_emails=set(), allowed_domains=set()
    )
    provider.verify_token = AsyncMock(return_value=_claims(ALLOWED, True))

    with pytest.raises(AccessDeniedError):
        await service.handle_oauth_callback(code="c", redirect_uri="https://x/cb")

    user_repo.create_user.assert_not_called()


async def test_access_denied_is_not_a_value_error():
    """Web handlers map ValueError to 400; a denial must stay distinguishable (403)."""
    assert not issubclass(AccessDeniedError, ValueError)


# ---------------------------------------------------------------------------
# link_oauth_identity
# ---------------------------------------------------------------------------


async def test_link_oauth_rejects_non_whitelisted_google_identity(
    service, provider, user_repo, account_repo
):
    provider.verify_token = AsyncMock(return_value=_claims("stranger@gmail.com", True))

    with pytest.raises(AccessDeniedError):
        await service.link_oauth_identity(user_id="u1", code="c", redirect_uri="https://x/cb")

    _assert_no_user_access(user_repo, account_repo)


async def test_link_oauth_allows_whitelisted_google_identity(service, provider, user_repo):
    user_repo.get_user = AsyncMock(
        return_value=UserProfile(user_id="u1", email=ALLOWED, account_id="a1")
    )
    provider.verify_token = AsyncMock(return_value=_claims(ALLOWED, True))

    user = await service.link_oauth_identity(user_id="u1", code="c", redirect_uri="https://x/cb")

    assert user.external_user_id == "firebase|sub-1"
    user_repo.update_user.assert_awaited_once()
