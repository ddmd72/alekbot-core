from uuid import uuid4
from typing import Optional
from datetime import datetime, timezone, timedelta

from ..domain.invite_code import InviteCode, InviteType
from ..ports.invite_code_repository import InviteCodeRepository
from ..ports.user_repository import UserRepository
from ..ports.account_repository import AccountRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..utils.logger import logger


class InviteCodeService:
    """
    Service for managing invite codes (generation, validation, consumption).
    
    Updated (2026-02-05): Added whitelist enforcement for Team Invites.
    """

    def __init__(
        self,
        invite_repo: InviteCodeRepository,
        user_repo: UserRepository,
        account_repo: AccountRepository,
        whitelist_repo: WhitelistRepository,
        code_ttl_days: int = 7
    ):
        self.repo = invite_repo
        self.user_repo = user_repo
        self.account_repo = account_repo
        self.whitelist_repo = whitelist_repo
        self.code_ttl_days = code_ttl_days

    async def _create_code(
        self,
        user_id: str,
        account_id: str,
        invite_type: InviteType,
        platform: Optional[str] = None,
        role: str = "MEMBER"
    ) -> InviteCode:
        """Internal helper to create and save an invite code."""
        # Generate a secure random code
        # Format: 3 segments of 3 chars (e.g. ABC-XYZ-123)
        # Using uuid4 for simplicity and entropy, taking first 12 chars roughly
        raw_uuid = str(uuid4()).upper().replace("-", "")
        code = f"{raw_uuid[:3]}-{raw_uuid[3:6]}-{raw_uuid[6:9]}"
        
        expires_at = datetime.now(timezone.utc) + timedelta(days=self.code_ttl_days)
        
        invite = InviteCode(
            code=code,
            user_id=user_id,
            account_id=account_id,
            type=invite_type,
            expires_at=expires_at,
            platform=platform,
            role=role
        )
        
        await self.repo.create(invite)
        logger.info(f"Generated {invite_type.value} code: {code} for user {user_id}")
        return invite

    async def generate_self_link(self, user_id: str, account_id: str, platform: str) -> InviteCode:
        """Generate a code to link a platform (Slack, etc) to the user's account."""
        if not platform:
            raise ValueError("Platform is required for self-link")
            
        return await self._create_code(
            user_id, account_id, InviteType.SELF_LINK, platform=platform
        )

    async def generate_team_invite(self, user_id: str, account_id: str, role: str = "MEMBER") -> InviteCode:
        """Generate a code to invite another user to join the team."""
        # Validate role (simple check for MVP)
        if role not in ["OWNER", "MEMBER", "VIEWER"]:
            raise ValueError(f"Invalid role: {role}")
            
        return await self._create_code(
            user_id, account_id, InviteType.TEAM_INVITE, role=role
        )

    async def validate_code(self, code: str) -> InviteCode:
        """Validate if a code exists and is active."""
        invite = await self.repo.get_by_code(code)
        if not invite:
            raise ValueError("Invalid invite code")
            
        if not invite.is_valid():
            raise ValueError("Invite code expired or already used")
            
        return invite

    async def consume_team_invite(self, code: str, new_member_user_id: str) -> None:
        """
        Consume a team invite code to join a user to an account.
        """
        logger.info(f"User {new_member_user_id} attempting to consume invite {code}")
        
        # 1. Validate code
        invite = await self.validate_code(code)
        
        if invite.type != InviteType.TEAM_INVITE:
            raise ValueError("This code is not for team invites")
            
        # Prevent self-invite (if user is already in this account)
        # We need to fetch the user to check their current account
        new_member = await self.user_repo.get_user(new_member_user_id)
        if not new_member:
            raise ValueError(f"User {new_member_user_id} not found")
            
        if new_member.account_id == invite.account_id:
            raise ValueError("User is already a member of this account")

        # 2. WHITELIST CHECK (NEW - 2026-02-05)
        # Enforce: ONLY whitelisted emails can join teams
        whitelist = await self.whitelist_repo.get_whitelist()
        
        if not whitelist.is_allowed(new_member.email):
            logger.warning(
                f"⛔ [InviteCodeService] User {new_member.email} NOT in whitelist "
                f"- team invite rejected"
            )
            raise ValueError(
                f"Your email ({new_member.email}) is not authorized to join teams. "
                "Contact admin for access."
            )
        
        logger.info(f"✅ [InviteCodeService] Whitelist check passed for {new_member.email}")

        # 3. Get Owner Account
        account = await self.account_repo.get_account(invite.account_id)
        if not account:
            raise ValueError(f"Target account {invite.account_id} not found")

        # 3. Update User's Account ID
        # Note: In MVP we overwrite the user's account.
        # If user had their own account with data, that data becomes "orphaned" or inaccessible
        # unless we migrate it. For MVP, we assume joining a team switches context.
        # Ideally we should warn user or migrate data.
        # For now, we strictly follow the requirement: join account.
        new_member.account_id = invite.account_id
        await self.user_repo.update_user(new_member)

        # 4. Update Account IAM Policy
        account.iam_policy[new_member_user_id] = invite.role
        await self.account_repo.update_account(account)

        # 5. Mark code as used
        invite.mark_used(new_member_user_id)
        await self.repo.update(invite)
        
        logger.info(f"User {new_member_user_id} joined account {invite.account_id} as {invite.role}")
