"""
Unit tests for FirestoreUserRepository OAuth methods (Session 7).

Tests for get_user_by_external_id() and link_platform_identity() methods.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.user import UserProfile, UserBotConfig
from src.config.environment import EnvironmentConfig


# ============================================================================
# Fixtures
# ============================================================================
@pytest.fixture
def env_config():
    """Create test environment config."""
    config = MagicMock(spec=EnvironmentConfig)
    config.firestore_collection_prefix = "test_"
    config.is_production = False
    return config


@pytest.fixture
def mock_db_client():
    """Create mock Firestore client."""
    return MagicMock()


@pytest.fixture
def mock_account_repo():
    """Create mock AccountRepository."""
    return AsyncMock()


@pytest.fixture
def user_repo(mock_db_client, env_config, mock_account_repo):
    """Create FirestoreUserRepository with mocks."""
    return FirestoreUserRepository(mock_db_client, env_config, mock_account_repo)


@pytest.fixture
def test_user():
    """Create test user with OAuth fields."""
    return UserProfile(
        user_id="user-123",
        external_user_id="firebase|abc123",
        email="test@example.com",
        display_name="Test User",
        account_id="account-456",
        platform_identities={},
    )


@pytest.fixture
def test_user_with_slack():
    """Create test user with Slack identity linked."""
    return UserProfile(
        user_id="user-789",
        external_user_id="firebase|xyz789",
        email="slack@example.com",
        display_name="Slack User",
        account_id="account-456",
        platform_identities={"slack": "U123456"},
    )


# ============================================================================
# get_user_by_external_id() Tests
# ============================================================================
@pytest.mark.asyncio
async def test_get_user_by_external_id_found(user_repo, mock_db_client, test_user):
    """Test getting user by external_id when user exists."""
    # Mock Firestore query
    mock_doc = MagicMock()
    mock_doc.id = test_user.user_id
    mock_doc.to_dict.return_value = test_user.model_dump()

    # Mock query stream
    async def mock_stream():
        yield mock_doc

    mock_query = MagicMock()
    mock_query.stream.return_value = mock_stream()

    # user_repo.users_col is stored at init time — patch it directly
    mock_collection = MagicMock()
    mock_collection.where.return_value.limit.return_value = mock_query
    user_repo.users_col = mock_collection

    # Execute
    result = await user_repo.get_user_by_external_id("firebase|abc123")

    # Verify
    assert result is not None
    assert result.user_id == test_user.user_id
    assert result.external_user_id == "firebase|abc123"
    assert result.email == test_user.email


@pytest.mark.asyncio
async def test_get_user_by_external_id_not_found(user_repo, mock_db_client):
    """Test getting user by external_id when user doesn't exist."""
    # Mock empty query result
    async def mock_stream():
        return
        yield  # Make this a generator

    mock_query = MagicMock()
    mock_query.stream.return_value = mock_stream()

    mock_collection = MagicMock()
    mock_collection.where.return_value.limit.return_value = mock_query
    mock_db_client.collection.return_value = mock_collection

    # Execute
    result = await user_repo.get_user_by_external_id("firebase|notfound")

    # Verify
    assert result is None


@pytest.mark.asyncio
async def test_get_user_by_external_id_query_format(user_repo, mock_db_client):
    """Test that query uses correct field and format."""
    # Mock query
    async def mock_stream():
        return
        yield

    mock_query = MagicMock()
    mock_query.stream.return_value = mock_stream()

    mock_limit = MagicMock(return_value=mock_query)
    mock_where = MagicMock()
    mock_where.limit = mock_limit

    # user_repo.users_col is stored at init time — patch it directly
    mock_collection = MagicMock()
    mock_collection.where.return_value = mock_where
    user_repo.users_col = mock_collection

    # Execute
    await user_repo.get_user_by_external_id("firebase|test123")

    # Verify query structure
    mock_collection.where.assert_called_once()
    mock_limit.assert_called_once_with(1)


# ============================================================================
# link_platform_identity() Tests
# ============================================================================
@pytest.mark.asyncio
async def test_link_platform_identity_success(user_repo, test_user):
    """Test linking platform identity to user."""
    # Mock get_user
    user_repo.get_user = AsyncMock(return_value=test_user)

    # Mock get_user_by_platform_id (no existing link)
    user_repo.get_user_by_platform_id = AsyncMock(return_value=None)

    # Mock Firestore set
    mock_doc_ref = MagicMock()
    mock_doc_ref.set = AsyncMock()
    user_repo.users_col.document.return_value = mock_doc_ref

    # Execute
    result = await user_repo.link_platform_identity(
        user_id="user-123",
        platform="slack",
        platform_user_id="U123456"
    )

    # Verify
    assert result.user_id == "user-123"
    assert result.platform_identities["slack"] == "U123456"
    mock_doc_ref.set.assert_called_once()


@pytest.mark.asyncio
async def test_link_platform_identity_user_not_found(user_repo):
    """Test linking platform identity when user doesn't exist."""
    # Mock get_user returns None
    user_repo.get_user = AsyncMock(return_value=None)

    # Execute and verify exception
    with pytest.raises(ValueError, match="User user-999 not found"):
        await user_repo.link_platform_identity(
            user_id="user-999",
            platform="slack",
            platform_user_id="U123456"
        )


@pytest.mark.asyncio
async def test_link_platform_identity_already_linked_to_another_user(
    user_repo, test_user, test_user_with_slack
):
    """Test linking platform identity that's already linked to another user."""
    # Mock get_user returns test_user
    user_repo.get_user = AsyncMock(return_value=test_user)

    # Mock get_user_by_platform_id returns different user
    user_repo.get_user_by_platform_id = AsyncMock(return_value=test_user_with_slack)

    # Execute and verify exception
    with pytest.raises(
        ValueError,
        match="Platform identity slack:U123456 already linked to user user-789"
    ):
        await user_repo.link_platform_identity(
            user_id="user-123",
            platform="slack",
            platform_user_id="U123456"
        )


@pytest.mark.asyncio
async def test_link_platform_identity_already_linked_to_same_user(
    user_repo, test_user_with_slack
):
    """Test relinking platform identity to same user (idempotent)."""
    # Mock get_user
    user_repo.get_user = AsyncMock(return_value=test_user_with_slack)

    # Mock get_user_by_platform_id returns same user
    user_repo.get_user_by_platform_id = AsyncMock(return_value=test_user_with_slack)

    # Mock Firestore set
    mock_doc_ref = MagicMock()
    mock_doc_ref.set = AsyncMock()
    user_repo.users_col.document.return_value = mock_doc_ref

    # Execute (should succeed - idempotent)
    result = await user_repo.link_platform_identity(
        user_id="user-789",
        platform="slack",
        platform_user_id="U123456"
    )

    # Verify
    assert result.user_id == "user-789"
    assert result.platform_identities["slack"] == "U123456"


@pytest.mark.asyncio
async def test_link_platform_identity_multiple_platforms(user_repo, test_user):
    """Test linking multiple platform identities to same user."""
    # Start with user that has no platforms
    user = test_user.model_copy()

    # Mock get_user
    user_repo.get_user = AsyncMock(return_value=user)

    # Mock get_user_by_platform_id (no conflicts)
    user_repo.get_user_by_platform_id = AsyncMock(return_value=None)

    # Mock Firestore set
    mock_doc_ref = MagicMock()
    mock_doc_ref.set = AsyncMock()
    user_repo.users_col.document.return_value = mock_doc_ref

    # Link Slack
    result1 = await user_repo.link_platform_identity(
        user_id="user-123",
        platform="slack",
        platform_user_id="U123456"
    )
    assert "slack" in result1.platform_identities

    # Link Telegram (update mock to return user with Slack)
    user_repo.get_user = AsyncMock(return_value=result1)

    result2 = await user_repo.link_platform_identity(
        user_id="user-123",
        platform="telegram",
        platform_user_id="T123456"
    )

    # Verify both platforms linked
    assert "slack" in result2.platform_identities
    assert "telegram" in result2.platform_identities
    assert result2.platform_identities["slack"] == "U123456"
    assert result2.platform_identities["telegram"] == "T123456"


@pytest.mark.asyncio
async def test_link_platform_identity_updates_timestamp(user_repo, test_user):
    """Test that linking updates user's updated_at timestamp."""
    # Mock get_user
    original_updated_at = test_user.updated_at
    user_repo.get_user = AsyncMock(return_value=test_user)

    # Mock get_user_by_platform_id
    user_repo.get_user_by_platform_id = AsyncMock(return_value=None)

    # Mock Firestore set
    mock_doc_ref = MagicMock()
    mock_doc_ref.set = AsyncMock()
    user_repo.users_col.document.return_value = mock_doc_ref

    # Execute
    result = await user_repo.link_platform_identity(
        user_id="user-123",
        platform="slack",
        platform_user_id="U123456"
    )

    # Verify timestamp was updated
    assert result.updated_at > original_updated_at


# ============================================================================
# Integration Tests (Query Patterns)
# ============================================================================
@pytest.mark.asyncio
async def test_oauth_flow_external_id_lookup(user_repo, test_user):
    """Test typical OAuth flow: lookup by external_id."""
    # Simulate OAuth callback: user signed in with Firebase
    # AuthenticationService will call get_user_by_external_id()

    # Mock Firestore query
    mock_doc = MagicMock()
    mock_doc.id = test_user.user_id
    mock_doc.to_dict.return_value = test_user.model_dump()

    async def mock_stream():
        yield mock_doc

    mock_query = MagicMock()
    mock_query.stream.return_value = mock_stream()

    # user_repo.users_col is stored at init time — patch it directly
    mock_collection = MagicMock()
    mock_collection.where.return_value.limit.return_value = mock_query
    user_repo.users_col = mock_collection

    # Execute
    user = await user_repo.get_user_by_external_id("firebase|abc123")

    # Verify
    assert user is not None
    assert user.external_user_id == "firebase|abc123"


@pytest.mark.asyncio
async def test_platform_linking_flow(user_repo, test_user):
    """Test typical platform linking flow: OAuth user links Slack."""
    # User authenticated via OAuth, now linking Slack

    # Mock get_user
    user_repo.get_user = AsyncMock(return_value=test_user)

    # Mock get_user_by_platform_id (Slack not linked yet)
    user_repo.get_user_by_platform_id = AsyncMock(return_value=None)

    # Mock Firestore set
    mock_doc_ref = MagicMock()
    mock_doc_ref.set = AsyncMock()
    user_repo.users_col.document.return_value = mock_doc_ref

    # Link Slack
    updated_user = await user_repo.link_platform_identity(
        user_id="user-123",
        platform="slack",
        platform_user_id="U123456"
    )

    # Verify
    assert updated_user.platform_identities["slack"] == "U123456"

    # Now user can be found via Slack ID
    user_repo.get_user_by_platform_id = AsyncMock(return_value=updated_user)
    found_user = await user_repo.get_user_by_platform_id("slack", "U123456")
    assert found_user.user_id == "user-123"
