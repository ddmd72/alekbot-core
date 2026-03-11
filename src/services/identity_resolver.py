from typing import Optional, TYPE_CHECKING
from ..domain.user import UserProfile
from ..domain.billing import BillingAccount
from ..domain.invite_code import InviteType
from ..ports.user_repository import UserRepository
from ..ports.account_repository import AccountRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .invite_code_service import InviteCodeService

class IdentityResolver:
    """
    Service to resolve platform-specific identities to internal UserProfile.
    Handles auto-registration for new users.
    """
    
    def __init__(
        self, 
        user_repo: UserRepository, 
        account_repo: AccountRepository,
        invite_service: Optional['InviteCodeService'] = None
    ):
        self.user_repo = user_repo
        self.account_repo = account_repo
        self.invite_service = invite_service

    async def resolve_user(
        self, 
        platform: str, 
        platform_user_id: str, 
        auto_create: bool = True,
        invite_code: Optional[str] = None
    ) -> UserProfile:
        """
        Resolve platform user to internal UserProfile.
        
        Args:
            platform: Platform name (e.g., "slack", "telegram")
            platform_user_id: Platform-specific user ID
            auto_create: Whether to create a new user if not found
            invite_code: Optional invite code to link account
            
        Returns:
            UserProfile instance
        """
        # 1. Try to find existing user
        user = await self.user_repo.get_user_by_platform_id(platform, platform_user_id)
        
        if user:
            logger.debug(f"🆔 Resolved user: {platform}:{platform_user_id} -> {user.user_id}")
            return user
            
        # 2. Try link with code if provided
        if invite_code and self.invite_service:
             linked_user = await self._try_link_with_code(platform, platform_user_id, invite_code)
             if linked_user:
                 logger.info(f"🔗 Linked user via code: {platform}:{platform_user_id} -> {linked_user.user_id}")
                 return linked_user

        # 3. If not found and auto_create is True, register new user
        if auto_create:
            logger.info(f"🆕 User not found. Auto-registering: {platform}:{platform_user_id}")
            return await self._register_new_user(platform, platform_user_id)
            
        raise ValueError(f"User not found for {platform}:{platform_user_id}")

    async def _try_link_with_code(self, platform: str, platform_user_id: str, code: str) -> Optional[UserProfile]:
        """Attempt to link platform identity using an invite code."""
        try:
            # Validate code
            invite = await self.invite_service.validate_code(code)
            
            if invite.type == InviteType.SELF_LINK:
                # Check platform match if enforced
                if invite.platform and invite.platform != platform:
                    logger.warning(f"Invite platform mismatch: {invite.platform} vs {platform}")
                    return None

                # Link this platform ID to the user who created the invite
                # Note: link_identity checks if platform_id is already taken, which we know it isn't (from step 1)
                user = await self.link_identity(invite.user_id, platform, platform_user_id)
                
                # Mark code as used
                invite.mark_used(user.user_id)
                await self.invite_service.repo.update(invite)
                
                return user
                
        except Exception as e:
            logger.warning(f"Failed to link with code {code}: {e}")
            return None
        return None

    async def _register_new_user(self, platform: str, platform_user_id: str) -> UserProfile:
        """Create a new user profile with default settings."""
        new_user = UserProfile(
            display_name=f"User {platform_user_id[:8]}",
            platform_identities={platform: platform_user_id}
        )

        account = BillingAccount(owner_user_id=new_user.user_id, member_user_ids=[new_user.user_id])
        await self.account_repo.create_account(account)
        new_user.account_id = account.account_id

        created_user = await self.user_repo.create_user(new_user)
        return created_user

    async def link_identity(self, user_id: str, platform: str, platform_user_id: str) -> UserProfile:
        """Link a new platform identity to an existing user."""
        user = await self.user_repo.get_user(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")
            
        # Check if identity is already taken
        existing = await self.user_repo.get_user_by_platform_id(platform, platform_user_id)
        if existing and existing.user_id != user_id:
            raise ValueError(f"Identity {platform}:{platform_user_id} is already linked to another user")
            
        user.platform_identities[platform] = platform_user_id
        return await self.user_repo.update_user(user)
