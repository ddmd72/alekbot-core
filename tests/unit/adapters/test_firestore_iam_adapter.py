"""
Unit tests for FirestoreIAMAdapter (OAuth Multi-Tenant Session 5).

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import pytest
from unittest.mock import AsyncMock, Mock

from src.adapters.firestore_iam_adapter import FirestoreIAMAdapter
from src.ports.iam_port import Role, ResourceType, Action
from src.domain.billing import BillingAccount, AccountTier


# ============================================================================
# Fixtures
# ============================================================================
@pytest.fixture
def mock_account_repo():
    """Mock AccountRepository."""
    return Mock()


@pytest.fixture
def iam_adapter(mock_account_repo):
    """Create FirestoreIAMAdapter with mocked repository."""
    return FirestoreIAMAdapter(mock_account_repo)


@pytest.fixture
def test_account():
    """Create test account with IAM policy."""
    return BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={
            "user-owner": "owner",
            "user-member": "member",
            "user-viewer": "viewer",
        },
    )


# ============================================================================
# Permission Checking Tests
# ============================================================================
@pytest.mark.asyncio
async def test_can_access_resource_owner_full_access(iam_adapter, mock_account_repo, test_account):
    """Test OWNER has full access to all resources."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    # OWNER can perform ADMIN actions
    assert await iam_adapter.can_access_resource(
        user_id="user-owner",
        resource_type=ResourceType.ACCOUNT,
        resource_id="account-1",
        action=Action.ADMIN,
        account_id="account-1",
    )

    # OWNER can DELETE resources
    assert await iam_adapter.can_access_resource(
        user_id="user-owner",
        resource_type=ResourceType.FACT,
        resource_id="fact-1",
        action=Action.DELETE,
        account_id="account-1",
    )


@pytest.mark.asyncio
async def test_can_access_resource_member_limited_access(iam_adapter, mock_account_repo, test_account):
    """Test MEMBER has read/write but no admin/delete."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    # MEMBER can READ account
    assert await iam_adapter.can_access_resource(
        user_id="user-member",
        resource_type=ResourceType.ACCOUNT,
        resource_id="account-1",
        action=Action.READ,
        account_id="account-1",
    )

    # MEMBER can WRITE facts
    assert await iam_adapter.can_access_resource(
        user_id="user-member",
        resource_type=ResourceType.FACT,
        resource_id="fact-1",
        action=Action.WRITE,
        account_id="account-1",
    )

    # MEMBER cannot perform ADMIN actions
    assert not await iam_adapter.can_access_resource(
        user_id="user-member",
        resource_type=ResourceType.ACCOUNT,
        resource_id="account-1",
        action=Action.ADMIN,
        account_id="account-1",
    )

    # MEMBER cannot DELETE facts
    assert not await iam_adapter.can_access_resource(
        user_id="user-member",
        resource_type=ResourceType.FACT,
        resource_id="fact-1",
        action=Action.DELETE,
        account_id="account-1",
    )


@pytest.mark.asyncio
async def test_can_access_resource_viewer_read_only(iam_adapter, mock_account_repo, test_account):
    """Test VIEWER has read-only access."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    # VIEWER can READ facts
    assert await iam_adapter.can_access_resource(
        user_id="user-viewer",
        resource_type=ResourceType.FACT,
        resource_id="fact-1",
        action=Action.READ,
        account_id="account-1",
    )

    # VIEWER cannot WRITE facts
    assert not await iam_adapter.can_access_resource(
        user_id="user-viewer",
        resource_type=ResourceType.FACT,
        resource_id="fact-1",
        action=Action.WRITE,
        account_id="account-1",
    )

    # VIEWER cannot DELETE
    assert not await iam_adapter.can_access_resource(
        user_id="user-viewer",
        resource_type=ResourceType.FACT,
        resource_id="fact-1",
        action=Action.DELETE,
        account_id="account-1",
    )


@pytest.mark.asyncio
async def test_can_access_resource_non_member_denied(iam_adapter, mock_account_repo, test_account):
    """Test non-member has no access."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    # Non-member cannot access any resource
    assert not await iam_adapter.can_access_resource(
        user_id="user-stranger",
        resource_type=ResourceType.FACT,
        resource_id="fact-1",
        action=Action.READ,
        account_id="account-1",
    )


@pytest.mark.asyncio
async def test_can_access_resource_no_account_id(iam_adapter):
    """Test access denied if no account_id provided."""
    assert not await iam_adapter.can_access_resource(
        user_id="user-1",
        resource_type=ResourceType.FACT,
        resource_id="fact-1",
        action=Action.READ,
        account_id=None,
    )


# ============================================================================
# Get User Role Tests
# ============================================================================
@pytest.mark.asyncio
async def test_get_user_role_success(iam_adapter, mock_account_repo, test_account):
    """Test getting user role from IAM policy."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    role = await iam_adapter.get_user_role("user-owner", "account-1")
    assert role == Role.OWNER

    role = await iam_adapter.get_user_role("user-member", "account-1")
    assert role == Role.MEMBER

    role = await iam_adapter.get_user_role("user-viewer", "account-1")
    assert role == Role.VIEWER


@pytest.mark.asyncio
async def test_get_user_role_non_member(iam_adapter, mock_account_repo, test_account):
    """Test getting role for non-member returns None."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    role = await iam_adapter.get_user_role("user-stranger", "account-1")
    assert role is None


@pytest.mark.asyncio
async def test_get_user_role_account_not_found(iam_adapter, mock_account_repo):
    """Test getting role raises error if account not found."""
    mock_account_repo.get_account = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="Account .* not found"):
        await iam_adapter.get_user_role("user-1", "nonexistent-account")


# ============================================================================
# Assign Role Tests
# ============================================================================
@pytest.mark.asyncio
async def test_assign_role_success(iam_adapter, mock_account_repo, test_account):
    """Test OWNER can assign roles to users."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)
    mock_account_repo.update_account = AsyncMock(return_value=test_account)

    # OWNER assigns MEMBER role to new user
    result = await iam_adapter.assign_role(
        user_id="user-new",
        account_id="account-1",
        role=Role.MEMBER,
        assigned_by="user-owner",
    )

    assert result is True
    mock_account_repo.update_account.assert_called_once()

    # Verify IAM policy was updated
    assert test_account.iam_policy["user-new"] == "member"


@pytest.mark.asyncio
async def test_assign_role_update_existing(iam_adapter, mock_account_repo, test_account):
    """Test OWNER can update existing user's role."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)
    mock_account_repo.update_account = AsyncMock(return_value=test_account)

    # OWNER promotes MEMBER to OWNER
    result = await iam_adapter.assign_role(
        user_id="user-member",
        account_id="account-1",
        role=Role.OWNER,
        assigned_by="user-owner",
    )

    assert result is True
    assert test_account.iam_policy["user-member"] == "owner"


@pytest.mark.asyncio
async def test_assign_role_permission_denied(iam_adapter, mock_account_repo, test_account):
    """Test non-OWNER cannot assign roles."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    # MEMBER tries to assign role
    with pytest.raises(PermissionError, match="is not OWNER"):
        await iam_adapter.assign_role(
            user_id="user-new",
            account_id="account-1",
            role=Role.MEMBER,
            assigned_by="user-member",
        )


# ============================================================================
# Revoke Access Tests
# ============================================================================
@pytest.mark.asyncio
async def test_revoke_access_success(iam_adapter, mock_account_repo, test_account):
    """Test OWNER can revoke access from users."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)
    mock_account_repo.update_account = AsyncMock(return_value=test_account)

    # OWNER revokes MEMBER access
    result = await iam_adapter.revoke_access(
        user_id="user-member",
        account_id="account-1",
        revoked_by="user-owner",
    )

    assert result is True
    assert "user-member" not in test_account.iam_policy


@pytest.mark.asyncio
async def test_revoke_access_sole_owner_protected(iam_adapter, mock_account_repo):
    """Test cannot revoke sole OWNER."""
    account_with_sole_owner = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"user-owner": "owner"},
    )

    mock_account_repo.get_account = AsyncMock(return_value=account_with_sole_owner)

    # Cannot revoke sole OWNER
    with pytest.raises(PermissionError, match="Cannot revoke sole OWNER"):
        await iam_adapter.revoke_access(
            user_id="user-owner",
            account_id="account-1",
            revoked_by="user-owner",
        )


@pytest.mark.asyncio
async def test_revoke_access_permission_denied(iam_adapter, mock_account_repo, test_account):
    """Test non-OWNER cannot revoke access."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    # MEMBER tries to revoke access
    with pytest.raises(PermissionError, match="is not OWNER"):
        await iam_adapter.revoke_access(
            user_id="user-viewer",
            account_id="account-1",
            revoked_by="user-member",
        )


@pytest.mark.asyncio
async def test_revoke_access_non_member(iam_adapter, mock_account_repo, test_account):
    """Test revoking non-member raises error."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    with pytest.raises(ValueError, match="is not a member"):
        await iam_adapter.revoke_access(
            user_id="user-stranger",
            account_id="account-1",
            revoked_by="user-owner",
        )


# ============================================================================
# Get Account Members Tests
# ============================================================================
@pytest.mark.asyncio
async def test_get_account_members_success(iam_adapter, mock_account_repo, test_account):
    """Test getting all account members with roles."""
    mock_account_repo.get_account = AsyncMock(return_value=test_account)

    members = await iam_adapter.get_account_members("account-1")

    assert len(members) == 3
    assert members["user-owner"] == Role.OWNER
    assert members["user-member"] == Role.MEMBER
    assert members["user-viewer"] == Role.VIEWER


@pytest.mark.asyncio
async def test_get_account_members_account_not_found(iam_adapter, mock_account_repo):
    """Test getting members raises error if account not found."""
    mock_account_repo.get_account = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="Account .* not found"):
        await iam_adapter.get_account_members("nonexistent-account")
