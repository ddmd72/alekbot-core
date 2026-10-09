import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

from src.services.invite_code_service import InviteCodeService
from src.domain.invite_code import InviteCode, InviteType
from src.domain.user import UserProfile
from src.domain.billing import BillingAccount
from src.domain.whitelist import WhitelistEntry
from src.ports.user_repository import UserRepository
from src.ports.account_repository import AccountRepository
from src.ports.invite_code_repository import InviteCodeRepository
from src.ports.whitelist_repository import WhitelistRepository

@pytest.fixture
def mock_user_repo():
    repo = AsyncMock(spec=UserRepository)
    repo.get_user = AsyncMock()
    repo.get_user_by_platform_id = AsyncMock()
    repo.update_user = AsyncMock(side_effect=lambda x: x)
    repo.create_user = AsyncMock(side_effect=lambda x: x)
    return repo

@pytest.fixture
def mock_account_repo():
    repo = AsyncMock(spec=AccountRepository)
    repo.create_account = AsyncMock(side_effect=lambda x: x)
    repo.get_account = AsyncMock()
    repo.update_account = AsyncMock(side_effect=lambda x: x)
    return repo

@pytest.fixture
def mock_whitelist_repo():
    repo = AsyncMock(spec=WhitelistRepository)
    whitelist = WhitelistEntry(allowed_emails=set(), allowed_domains={"test.com"})
    repo.get_whitelist = AsyncMock(return_value=whitelist)
    return repo

@pytest.fixture
def mock_invite_repo():
    repo = AsyncMock(spec=InviteCodeRepository)
    # Use a dict to simulate DB for invites
    invites = {}
    
    async def create(invite):
        invites[invite.code] = invite
        return invite
        
    async def get_by_code(code):
        return invites.get(code)
        
    async def update(invite):
        invites[invite.code] = invite
        return invite
        
    repo.create = AsyncMock(side_effect=create)
    repo.get_by_code = AsyncMock(side_effect=get_by_code)
    repo.update = AsyncMock(side_effect=update)
    repo.list_by_user = AsyncMock()
    return repo

@pytest.fixture
def invite_service(mock_invite_repo, mock_user_repo, mock_account_repo, mock_whitelist_repo):
    return InviteCodeService(mock_invite_repo, mock_user_repo, mock_account_repo, mock_whitelist_repo)

@pytest.mark.asyncio
async def test_team_invite_consumption(invite_service, mock_user_repo, mock_account_repo):
    """
    E2E Flow (Web):
    1. Owner generates TEAM_INVITE.
    2. New User consumes code.
    3. New User added to Owner's Account.
    """
    # 1. Setup Owner and Account
    owner = UserProfile(user_id="owner", account_id="acc-owner")
    account = BillingAccount(account_id="acc-owner", iam_policy={"owner": "OWNER"})
    
    mock_user_repo.get_user.side_effect = lambda uid: owner if uid == "owner" else None
    mock_account_repo.get_account.return_value = account
    
    # 2. Generate Team Invite
    code = await invite_service.generate_team_invite("owner", "acc-owner", "MEMBER")
    
    # 3. Setup Joining User (currently in their own default account)
    joiner = UserProfile(user_id="joiner", account_id="acc-joiner-default", email="joiner@test.com")
    mock_user_repo.get_user.side_effect = lambda uid: joiner if uid == "joiner" else (owner if uid == "owner" else None)
    
    # 4. Consume Invite
    await invite_service.consume_team_invite(code.code, "joiner")
    
    # 5. Verify
    # Joiner's account_id updated
    updated_joiner = mock_user_repo.update_user.call_args[0][0]
    assert updated_joiner.account_id == "acc-owner"
    
    # Account IAM policy updated
    updated_account = mock_account_repo.update_account.call_args[0][0]
    assert updated_account.iam_policy["joiner"] == "MEMBER"
    
    # Code marked used
    stored_invite = await invite_service.repo.get_by_code(code.code)
    assert stored_invite.used_at is not None
