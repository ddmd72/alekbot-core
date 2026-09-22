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
# link_platform_identity() transactional test helpers
#
# link_platform_identity runs inside a Firestore transaction (closes a TOCTOU
# race — see src/adapters/firestore_user_repo.py). Mirrors the passthrough-patch
# technique from tests/unit/adapters/test_firestore_account_repo.py
# (_make_repo_and_capture / _passthrough_transactional): patch
# firestore.async_transactional to a bare passthrough so the transactional inner
# function runs directly against a plain MagicMock() transaction, with no need
# to stub the real SDK's transaction lifecycle.
# ============================================================================
def _passthrough_transactional(fn):
    return fn


def _async_iter(items):
    async def _gen():
        for item in items:
            yield item
    return _gen()


def _setup_link_mocks(user_repo, *, existing_user_dict, conflict_doc_id=None):
    """Wire users_col + db.transaction for one link_platform_identity call.

    existing_user_dict: dict returned by the user doc's snapshot.to_dict(), or
        None to simulate "user not found".
    conflict_doc_id: if set, the conflict query yields one doc with this id
        (simulating an existing platform-id binding); None means no conflict.
    """
    snapshot = MagicMock()
    snapshot.exists = existing_user_dict is not None
    if existing_user_dict is not None:
        snapshot.to_dict.return_value = existing_user_dict

    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=snapshot)

    conflict_docs = []
    if conflict_doc_id is not None:
        conflict_doc = MagicMock()
        conflict_doc.id = conflict_doc_id
        conflict_docs = [conflict_doc]

    query = MagicMock()
    query.stream.return_value = _async_iter(conflict_docs)

    user_repo.users_col.document.return_value = doc_ref
    user_repo.users_col.where.return_value.limit.return_value = query

    transaction = MagicMock()
    user_repo.db.transaction.return_value = transaction

    return doc_ref, transaction


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
    doc_ref, transaction = _setup_link_mocks(
        user_repo, existing_user_dict=test_user.model_dump(), conflict_doc_id=None
    )

    with patch(
        "src.adapters.firestore_user_repo.firestore.async_transactional",
        _passthrough_transactional,
    ):
        result = await user_repo.link_platform_identity(
            user_id="user-123", platform="slack", platform_user_id="U123456"
        )

    assert result.user_id == "user-123"
    assert result.platform_identities["slack"] == "U123456"
    transaction.set.assert_called_once()
    written_ref, written_data = transaction.set.call_args.args[:2]
    assert written_ref is doc_ref
    assert written_data["platform_identities"]["slack"] == "U123456"


@pytest.mark.asyncio
async def test_link_platform_identity_user_not_found(user_repo):
    """Test linking platform identity when user doesn't exist."""
    _setup_link_mocks(user_repo, existing_user_dict=None, conflict_doc_id=None)

    with patch(
        "src.adapters.firestore_user_repo.firestore.async_transactional",
        _passthrough_transactional,
    ):
        with pytest.raises(ValueError, match="User user-999 not found"):
            await user_repo.link_platform_identity(
                user_id="user-999", platform="slack", platform_user_id="U123456"
            )


@pytest.mark.asyncio
async def test_link_platform_identity_already_linked_to_another_user(
    user_repo, test_user
):
    """Test linking platform identity that's already linked to another user."""
    _setup_link_mocks(
        user_repo, existing_user_dict=test_user.model_dump(), conflict_doc_id="user-789"
    )

    with patch(
        "src.adapters.firestore_user_repo.firestore.async_transactional",
        _passthrough_transactional,
    ):
        with pytest.raises(
            ValueError,
            match="Platform identity slack:U123456 already linked to user user-789"
        ):
            await user_repo.link_platform_identity(
                user_id="user-123", platform="slack", platform_user_id="U123456"
            )


@pytest.mark.asyncio
async def test_link_platform_identity_already_linked_to_same_user(
    user_repo, test_user_with_slack
):
    """Test relinking platform identity to same user (idempotent)."""
    # conflict_doc_id == the same user_id we're linking -> not a real conflict
    doc_ref, transaction = _setup_link_mocks(
        user_repo,
        existing_user_dict=test_user_with_slack.model_dump(),
        conflict_doc_id="user-789",
    )

    with patch(
        "src.adapters.firestore_user_repo.firestore.async_transactional",
        _passthrough_transactional,
    ):
        result = await user_repo.link_platform_identity(
            user_id="user-789", platform="slack", platform_user_id="U123456"
        )

    assert result.user_id == "user-789"
    assert result.platform_identities["slack"] == "U123456"
    transaction.set.assert_called_once()


@pytest.mark.asyncio
async def test_link_platform_identity_multiple_platforms(user_repo, test_user):
    """Test linking multiple platform identities to same user."""
    user = test_user.model_copy()

    # First link: Slack
    _setup_link_mocks(user_repo, existing_user_dict=user.model_dump(), conflict_doc_id=None)
    with patch(
        "src.adapters.firestore_user_repo.firestore.async_transactional",
        _passthrough_transactional,
    ):
        result1 = await user_repo.link_platform_identity(
            user_id="user-123", platform="slack", platform_user_id="U123456"
        )
    assert "slack" in result1.platform_identities

    # Second link: Telegram — the read now reflects result1's state (simulating
    # that the first write actually landed), so both platforms survive.
    _setup_link_mocks(
        user_repo, existing_user_dict=result1.model_dump(), conflict_doc_id=None
    )
    with patch(
        "src.adapters.firestore_user_repo.firestore.async_transactional",
        _passthrough_transactional,
    ):
        result2 = await user_repo.link_platform_identity(
            user_id="user-123", platform="telegram", platform_user_id="T123456"
        )

    assert "slack" in result2.platform_identities
    assert "telegram" in result2.platform_identities
    assert result2.platform_identities["slack"] == "U123456"
    assert result2.platform_identities["telegram"] == "T123456"


@pytest.mark.asyncio
async def test_link_platform_identity_updates_timestamp(user_repo, test_user):
    """Test that linking updates user's updated_at timestamp."""
    original_updated_at = test_user.updated_at
    _setup_link_mocks(
        user_repo, existing_user_dict=test_user.model_dump(), conflict_doc_id=None
    )

    with patch(
        "src.adapters.firestore_user_repo.firestore.async_transactional",
        _passthrough_transactional,
    ):
        result = await user_repo.link_platform_identity(
            user_id="user-123", platform="slack", platform_user_id="U123456"
        )

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
    _setup_link_mocks(
        user_repo, existing_user_dict=test_user.model_dump(), conflict_doc_id=None
    )

    with patch(
        "src.adapters.firestore_user_repo.firestore.async_transactional",
        _passthrough_transactional,
    ):
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
