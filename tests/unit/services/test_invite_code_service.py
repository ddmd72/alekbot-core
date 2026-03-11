import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta

from src.services.invite_code_service import InviteCodeService
from src.domain.invite_code import InviteCode, InviteType
from src.domain.user import UserProfile
from src.domain.billing import BillingAccount


@pytest.fixture
def mock_invite_repo():
    repo = AsyncMock()
    repo.create = AsyncMock(side_effect=lambda x: x)
    repo.update = AsyncMock(side_effect=lambda x: x)
    return repo

@pytest.fixture
def mock_user_repo():
    repo = AsyncMock()
    repo.update_user = AsyncMock(side_effect=lambda x: x)
    return repo

@pytest.fixture
def mock_account_repo():
    repo = AsyncMock()
    repo.update_account = AsyncMock(side_effect=lambda x: x)
    return repo

@pytest.fixture
def mock_whitelist_repo():
    return AsyncMock()

@pytest.fixture
def service(mock_invite_repo, mock_user_repo, mock_account_repo, mock_whitelist_repo):
    return InviteCodeService(mock_invite_repo, mock_user_repo, mock_account_repo, mock_whitelist_repo)

@pytest.mark.asyncio
async def test_generate_self_link(service, mock_invite_repo):
    code = await service.generate_self_link("u1", "a1", "slack")
    
    assert code.type == InviteType.SELF_LINK
    assert code.platform == "slack"
    assert code.user_id == "u1"
    mock_invite_repo.create.assert_called_once()
    created = mock_invite_repo.create.call_args[0][0]
    assert created.user_id == "u1"
    assert created.account_id == "a1"
    assert created.platform == "slack"

@pytest.mark.asyncio
async def test_generate_team_invite(service, mock_invite_repo):
    code = await service.generate_team_invite("u1", "a1", "MEMBER")
    
    assert code.type == InviteType.TEAM_INVITE
    assert code.role == "MEMBER"
    mock_invite_repo.create.assert_called_once()
    created = mock_invite_repo.create.call_args[0][0]
    assert created.user_id == "u1"
    assert created.account_id == "a1"
    assert created.role == "MEMBER"

@pytest.mark.asyncio
async def test_validate_code_valid(service, mock_invite_repo):
    invite = InviteCode(
        code="ABC", user_id="u1", account_id="a1", type=InviteType.SELF_LINK,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )
    mock_invite_repo.get_by_code.return_value = invite
    
    result = await service.validate_code("ABC")
    assert result == invite

@pytest.mark.asyncio
async def test_validate_code_invalid(service, mock_invite_repo):
    mock_invite_repo.get_by_code.return_value = None
    
    with pytest.raises(ValueError, match="Invalid invite code"):
        await service.validate_code("ABC")

@pytest.mark.asyncio
async def test_consume_team_invite_success(service, mock_invite_repo, mock_user_repo, mock_account_repo):
    # Setup Invite
    invite = InviteCode(
        code="INV-123", user_id="owner", account_id="acc-target", 
        type=InviteType.TEAM_INVITE, role="MEMBER",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )
    mock_invite_repo.get_by_code.return_value = invite
    
    # Setup User (New Member)
    user = UserProfile(user_id="new-member", account_id="acc-old")
    mock_user_repo.get_user.return_value = user
    
    # Setup Account (Target)
    account = BillingAccount(account_id="acc-target", iam_policy={"owner": "OWNER"})
    mock_account_repo.get_account.return_value = account
    
    # Execute
    await service.consume_team_invite("INV-123", "new-member")
    
    # Verify User Updated
    mock_user_repo.update_user.assert_called_once()
    updated_user = mock_user_repo.update_user.call_args[0][0]
    assert updated_user.account_id == "acc-target"
    
    # Verify Account IAM Updated
    mock_account_repo.update_account.assert_called_once()
    updated_account = mock_account_repo.update_account.call_args[0][0]
    assert updated_account.iam_policy["new-member"] == "MEMBER"
    
    # Verify Invite Marked Used
    mock_invite_repo.update.assert_called_once()
    updated_invite = mock_invite_repo.update.call_args[0][0]
    assert updated_invite.used_by_user_id == "new-member"

@pytest.mark.asyncio
async def test_consume_team_invite_already_member(service, mock_invite_repo, mock_user_repo):
    invite = InviteCode(
        code="INV-123", user_id="owner", account_id="acc-target", 
        type=InviteType.TEAM_INVITE,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )
    mock_invite_repo.get_by_code.return_value = invite
    
    user = UserProfile(user_id="member", account_id="acc-target") # Already in account
    mock_user_repo.get_user.return_value = user
    
    with pytest.raises(ValueError, match="User is already a member"):
        await service.consume_team_invite("INV-123", "member")
