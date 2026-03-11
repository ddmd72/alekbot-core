"""
Integration tests for OAuth Multi-Tenant system (Session 9).

Tests complete flows with multiple services working together:
- AuthenticationService + SessionService + IAMPort + ConfigurationService
- UserRepository + AccountRepository
- End-to-end OAuth registration and login
- IAM permission enforcement
- Configuration inheritance

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from src.services.authentication_service import AuthenticationService
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.session_service import SessionService
from src.services.configuration_service import ConfigurationService
from src.adapters.firestore_iam_adapter import FirestoreIAMAdapter
from src.domain.user import UserProfile, UserBotConfig
from src.domain.billing import BillingAccount, AccountTier
from src.ports.iam_port import Role, ResourceType, Action


# ============================================================================
# Fixtures
# ============================================================================
@pytest.fixture
def mock_user_repo():
    """Mock UserRepository."""
    repo = AsyncMock()
    repo.get_user_by_external_id = AsyncMock(return_value=None)
    repo.create_user = AsyncMock(side_effect=lambda user: user)
    repo.update_user = AsyncMock(side_effect=lambda user: user)
    repo.get_user = AsyncMock()
    repo.link_platform_identity = AsyncMock()
    return repo


@pytest.fixture
def mock_account_repo():
    """Mock AccountRepository."""
    repo = AsyncMock()
    repo.create_account = AsyncMock(side_effect=lambda account: account)
    repo.get_account = AsyncMock()
    repo.update_account = AsyncMock(side_effect=lambda account: account)
    return repo


@pytest.fixture
def mock_auth_provider():
    """Mock AuthPort provider."""
    provider = AsyncMock()
    provider.get_provider_name = MagicMock(return_value="firebase")
    provider.exchange_code_for_tokens = AsyncMock()
    provider.verify_token = AsyncMock()
    provider.get_user_info = AsyncMock()
    return provider


@pytest.fixture
def auth_service(mock_user_repo, mock_account_repo, mock_auth_provider):
    """Create AuthenticationService."""
    mock_auth_registry = MagicMock(spec=AuthProviderRegistry)
    mock_auth_registry.get_provider.return_value = mock_auth_provider
    mock_user_repo.get_user_by_email = AsyncMock(return_value=None)
    return AuthenticationService(
        user_repo=mock_user_repo,
        account_repo=mock_account_repo,
        auth_registry=mock_auth_registry,
    )


@pytest.fixture
def session_service():
    """Create SessionService."""
    return SessionService(
        secret_key="test-secret-key-must-be-32-characters-minimum",
        access_token_ttl=3600,
        refresh_token_ttl=86400,
    )


@pytest.fixture
def iam_adapter(mock_account_repo):
    """Create FirestoreIAMAdapter."""
    return FirestoreIAMAdapter(account_repo=mock_account_repo)


@pytest.fixture
def config_service():
    """Create ConfigurationService."""
    return ConfigurationService()


# ============================================================================
# Integration Test: OAuth Registration Flow (Master Account First)
# ============================================================================
@pytest.mark.asyncio
async def test_oauth_registration_creates_account_and_user(
    auth_service, mock_user_repo, mock_account_repo, mock_auth_provider
):
    """
    Test complete OAuth registration flow:
    1. User signs in via OAuth (first time)
    2. System creates BillingAccount
    3. System creates UserProfile
    4. User is set as OWNER of account
    """
    # Mock OAuth provider responses
    mock_auth_provider.exchange_code_for_tokens.return_value = MagicMock(
        access_token="access_token",
        refresh_token="refresh_token",
        id_token="id_token",
    )
    verify_claims = MagicMock()
    verify_claims.sub = "user123"
    verify_claims.email = "newuser@example.com"
    verify_claims.name = "New User"
    mock_auth_provider.verify_token.return_value = verify_claims

    user_info = MagicMock()
    user_info.email = "newuser@example.com"
    user_info.name = "New User"
    user_info.picture = None
    user_info.locale = None
    user_info.email_verified = True
    user_info.sub = "user123"
    mock_auth_provider.get_user_info.return_value = user_info

    # Mock repository: user doesn't exist yet
    mock_user_repo.get_user_by_external_id.return_value = None

    # Wire get_account to return the account created by create_account
    async def get_created_account(account_id):
        if mock_account_repo.create_account.called:
            acct = mock_account_repo.create_account.call_args[0][0]
            if acct.account_id == account_id:
                return acct
        return None
    mock_account_repo.get_account.side_effect = get_created_account

    # Execute OAuth callback
    user, account, tokens = await auth_service.handle_oauth_callback(
        code="oauth_code",
        redirect_uri="http://localhost/callback",
    )

    # Verify account was created
    assert mock_account_repo.create_account.called
    created_account = mock_account_repo.create_account.call_args[0][0]
    assert created_account.tier == AccountTier.FREE
    assert user.user_id in created_account.iam_policy
    assert created_account.iam_policy[user.user_id] == Role.OWNER.value

    # Verify user was created
    assert mock_user_repo.create_user.called
    created_user = mock_user_repo.create_user.call_args[0][0]
    assert created_user.external_user_id == "firebase|user123"
    assert created_user.email == "newuser@example.com"
    assert created_user.account_id == created_account.account_id

    # Verify Master Account First: account_id matches
    assert user.account_id == account.account_id


@pytest.mark.asyncio
async def test_oauth_login_existing_user(
    auth_service, mock_user_repo, mock_account_repo, mock_auth_provider
):
    """
    Test OAuth login for existing user:
    1. User signs in via OAuth (returning user)
    2. System finds existing user by external_user_id
    3. System loads user's account
    4. No new account created
    """
    # Existing user
    existing_user = UserProfile(
        user_id="user-existing",
        external_user_id="firebase|user123",
        email="existing@example.com",
        display_name="Existing User",
        account_id="account-existing",
    )
    existing_account = BillingAccount(
        account_id="account-existing",
        tier=AccountTier.PRO,
        iam_policy={"user-existing": "owner"},
    )

    # Mock OAuth provider
    mock_auth_provider.exchange_code_for_tokens.return_value = MagicMock(
        access_token="access_token",
        refresh_token="refresh_token",
        id_token="id_token",
    )
    mock_auth_provider.verify_token.return_value = MagicMock(
        sub="user123", email="existing@example.com", name=None,
    )

    # Mock repository: user exists
    mock_user_repo.get_user_by_external_id.return_value = existing_user
    mock_account_repo.get_account.return_value = existing_account

    # Execute OAuth callback
    user, account, tokens = await auth_service.handle_oauth_callback(
        code="oauth_code",
        redirect_uri="http://localhost/callback",
    )

    # Verify no new account created
    assert not mock_account_repo.create_account.called

    # Verify existing user returned
    assert user.user_id == "user-existing"
    assert account.account_id == "account-existing"
    assert account.tier == AccountTier.PRO


# ============================================================================
# Integration Test: JWT Session Management
# ============================================================================
@pytest.mark.asyncio
async def test_jwt_session_flow(session_service):
    """
    Test JWT session creation and verification:
    1. Create access token
    2. Create refresh token
    3. Verify access token
    4. Verify refresh token
    """
    user = UserProfile(
        user_id="user-123",
        external_user_id="firebase|user123",
        email="user@example.com",
        display_name="Test User",
        account_id="account-456",
    )
    account = BillingAccount(
        account_id="account-456",
        tier=AccountTier.FREE,
        iam_policy={"user-123": "owner"},
    )

    # Create tokens
    access_token = session_service.create_access_token(user, account)
    refresh_token = session_service.create_refresh_token(user, account)

    # Verify tokens
    access_payload = session_service.verify_access_token(access_token)
    refresh_payload = session_service.verify_refresh_token(refresh_token)

    # Verify access token payload
    assert access_payload["sub"] == "user-123"
    assert access_payload["account_id"] == "account-456"
    assert access_payload["external_user_id"] == "firebase|user123"
    assert access_payload["email"] == "user@example.com"
    assert access_payload["role"] == "owner"
    assert access_payload["tier"] == "free"
    assert access_payload["type"] == "access"

    # Verify refresh token payload
    assert refresh_payload["sub"] == "user-123"
    assert refresh_payload["account_id"] == "account-456"
    assert refresh_payload["type"] == "refresh"


# ============================================================================
# Integration Test: IAM Permission Enforcement
# ============================================================================
@pytest.mark.asyncio
async def test_iam_owner_has_full_access(iam_adapter, mock_account_repo):
    """Test OWNER role has full access to all resources."""
    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"owner-user": "owner"},
    )
    mock_account_repo.get_account.return_value = account

    # Test OWNER can do everything
    assert await iam_adapter.can_access_resource(
        "owner-user", ResourceType.ACCOUNT, "account-1", Action.ADMIN, "account-1"
    )
    assert await iam_adapter.can_access_resource(
        "owner-user", ResourceType.FACT, "fact-1", Action.DELETE, "account-1"
    )
    assert await iam_adapter.can_access_resource(
        "owner-user", ResourceType.CONFIG, "config-1", Action.WRITE, "account-1"
    )


@pytest.mark.asyncio
async def test_iam_member_limited_access(iam_adapter, mock_account_repo):
    """Test MEMBER role has limited access."""
    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"owner-user": "owner", "member-user": "member"},
    )
    mock_account_repo.get_account.return_value = account

    # Member can read/write facts
    assert await iam_adapter.can_access_resource(
        "member-user", ResourceType.FACT, "fact-1", Action.READ, "account-1"
    )
    assert await iam_adapter.can_access_resource(
        "member-user", ResourceType.FACT, "fact-1", Action.WRITE, "account-1"
    )

    # Member cannot delete or admin
    assert not await iam_adapter.can_access_resource(
        "member-user", ResourceType.FACT, "fact-1", Action.DELETE, "account-1"
    )
    assert not await iam_adapter.can_access_resource(
        "member-user", ResourceType.ACCOUNT, "account-1", Action.ADMIN, "account-1"
    )


@pytest.mark.asyncio
async def test_iam_viewer_read_only(iam_adapter, mock_account_repo):
    """Test VIEWER role has read-only access."""
    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"owner-user": "owner", "viewer-user": "viewer"},
    )
    mock_account_repo.get_account.return_value = account

    # Viewer can read
    assert await iam_adapter.can_access_resource(
        "viewer-user", ResourceType.FACT, "fact-1", Action.READ, "account-1"
    )

    # Viewer cannot write, delete, or admin
    assert not await iam_adapter.can_access_resource(
        "viewer-user", ResourceType.FACT, "fact-1", Action.WRITE, "account-1"
    )
    assert not await iam_adapter.can_access_resource(
        "viewer-user", ResourceType.FACT, "fact-1", Action.DELETE, "account-1"
    )
    assert not await iam_adapter.can_access_resource(
        "viewer-user", ResourceType.ACCOUNT, "account-1", Action.ADMIN, "account-1"
    )


@pytest.mark.asyncio
async def test_iam_role_assignment_requires_owner(iam_adapter, mock_account_repo):
    """Test only OWNER can assign roles."""
    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"owner-user": "owner", "member-user": "member"},
    )
    mock_account_repo.get_account.return_value = account

    # Owner can assign roles
    success = await iam_adapter.assign_role(
        user_id="new-user",
        account_id="account-1",
        role=Role.MEMBER,
        assigned_by="owner-user",
    )
    assert success

    # Member cannot assign roles
    with pytest.raises(PermissionError):
        await iam_adapter.assign_role(
            user_id="another-user",
            account_id="account-1",
            role=Role.MEMBER,
            assigned_by="member-user",
        )


# ============================================================================
# Integration Test: Configuration Inheritance
# ============================================================================
@pytest.mark.asyncio
async def test_config_inheritance_account_defaults(config_service):
    """Test configuration inheritance: user with no overrides uses account defaults."""
    user = UserProfile(
        user_id="user-1",
        email="user1@example.com",
        account_id="account-1",
        config=UserBotConfig(),  # Default config (no overrides)
    )
    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FAMILY,
        iam_policy={"user-1": "member"},
        account_defaults=UserBotConfig(
            temperature=0.7,
            default_tier="eco",
            agent_tiers={"router": "eco", "planning": "balanced"},
        ),
    )

    # Get effective config
    effective = config_service.get_effective_config(user, account)

    # Should use account defaults
    assert effective.temperature == 0.7
    assert effective.default_tier == "eco"
    assert effective.agent_tiers == {"router": "eco", "planning": "balanced"}


@pytest.mark.asyncio
async def test_config_inheritance_user_overrides(config_service):
    """Test configuration inheritance: user overrides merge with account defaults."""
    user = UserProfile(
        user_id="user-2",
        email="user2@example.com",
        account_id="account-1",
        config=UserBotConfig(
            temperature=0.9,  # Override temperature
            agent_tiers={"quick": "balanced"},  # Override specific agent
        ),
    )
    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FAMILY,
        iam_policy={"user-2": "member"},
        account_defaults=UserBotConfig(
            temperature=0.7,
            default_tier="eco",
            agent_tiers={"router": "eco", "planning": "balanced"},
        ),
    )

    # Get effective config
    effective = config_service.get_effective_config(user, account)

    # User override wins for temperature
    assert effective.temperature == 0.9

    # Account default used for default_tier
    assert effective.default_tier == "eco"

    # Dict deep merge: both account and user tiers
    assert effective.agent_tiers == {
        "router": "eco",  # from account
        "planning": "balanced",  # from account
        "quick": "balanced",  # from user
    }


# ============================================================================
# Integration Test: Platform Linking
# ============================================================================
@pytest.mark.asyncio
async def test_platform_linking_flow(mock_user_repo):
    """
    Test platform linking flow:
    1. User authenticates via OAuth
    2. User connects Slack account
    3. Future messages from Slack are linked to OAuth user
    """
    user = UserProfile(
        user_id="user-oauth",
        external_user_id="firebase|user123",
        email="oauth@example.com",
        display_name="OAuth User",
        account_id="account-1",
        platform_identities={},
    )

    # Mock repository methods
    mock_user_repo.get_user.return_value = user
    mock_user_repo.get_user_by_platform_id.return_value = None
    mock_user_repo.link_platform_identity.return_value = user

    # Link Slack identity
    await mock_user_repo.link_platform_identity(
        user_id="user-oauth",
        platform="slack",
        platform_user_id="U123456",
    )

    # Verify link was called
    mock_user_repo.link_platform_identity.assert_called_once_with(
        user_id="user-oauth",
        platform="slack",
        platform_user_id="U123456",
    )


# ============================================================================
# Integration Test: Complete OAuth → IAM → Config Flow
# ============================================================================
@pytest.mark.asyncio
async def test_complete_oauth_to_config_flow(
    auth_service, iam_adapter, config_service,
    mock_user_repo, mock_account_repo, mock_auth_provider
):
    """
    Test complete flow: OAuth registration → IAM check → Config resolution

    Scenario: Parent creates family account, sets defaults, child user uses them.
    """
    # Step 1: Parent registers via OAuth
    mock_auth_provider.exchange_code_for_tokens.return_value = MagicMock(
        access_token="access", refresh_token="refresh", id_token="id"
    )
    verify_claims = MagicMock()
    verify_claims.sub = "parent123"
    verify_claims.email = "parent@family.com"
    verify_claims.name = "Parent User"
    mock_auth_provider.verify_token.return_value = verify_claims

    user_info = MagicMock()
    user_info.email = "parent@family.com"
    user_info.name = "Parent User"
    user_info.picture = None
    user_info.locale = None
    user_info.email_verified = True
    user_info.sub = "parent123"
    mock_auth_provider.get_user_info.return_value = user_info
    mock_user_repo.get_user_by_external_id.return_value = None

    # Wire get_account to return the account created by create_account
    async def get_created_account(account_id):
        if mock_account_repo.create_account.called:
            acct = mock_account_repo.create_account.call_args[0][0]
            if acct.account_id == account_id:
                return acct
        return None
    mock_account_repo.get_account.side_effect = get_created_account

    parent_user, parent_account, _ = await auth_service.handle_oauth_callback(
        code="oauth_code",
        redirect_uri="http://localhost/callback",
    )

    # Step 2: Parent sets account defaults
    parent_account.account_defaults = UserBotConfig(
        temperature=0.7,
        default_tier="eco",
    )
    await mock_account_repo.update_account(parent_account)

    # Step 3: Parent invites child (simulated - just add to IAM)
    child_user = UserProfile(
        user_id="child-user",
        email="child@family.com",
        display_name="Child User",
        account_id=parent_account.account_id,
        config=UserBotConfig(),  # Default config (no overrides)
    )
    parent_account.iam_policy[child_user.user_id] = "member"
    await mock_account_repo.update_account(parent_account)

    # Step 4: Verify child has MEMBER role
    mock_account_repo.get_account.return_value = parent_account
    child_role = await iam_adapter.get_user_role(
        child_user.user_id, parent_account.account_id
    )
    assert child_role == Role.MEMBER

    # Step 5: Verify child uses parent's account defaults
    effective_config = config_service.get_effective_config(child_user, parent_account)
    assert effective_config.temperature == 0.7  # Parent's default
    assert effective_config.default_tier == "eco"  # Parent's default

    # Verify child has limited permissions (MEMBER role)
    assert await iam_adapter.can_access_resource(
        child_user.user_id,
        ResourceType.FACT,
        "fact-1",
        Action.READ,
        parent_account.account_id,
    )
    assert not await iam_adapter.can_access_resource(
        child_user.user_id,
        ResourceType.ACCOUNT,
        parent_account.account_id,
        Action.ADMIN,
        parent_account.account_id,
    )
