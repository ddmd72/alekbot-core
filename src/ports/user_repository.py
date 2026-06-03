from abc import ABC, abstractmethod
from typing import Optional
from ..domain.user import UserProfile

class UserRepository(ABC):
    """
    Abstract interface for User Registry operations.
    Follows Hexagonal Architecture (Port).
    """
    
    @abstractmethod
    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        """Retrieve user by internal UUID."""
        pass
    
    @abstractmethod
    async def get_user_by_platform_id(self, platform: str, platform_user_id: str) -> Optional[UserProfile]:
        """
        Retrieve user by platform-specific ID (e.g., Slack ID).
        Used for identity resolution.
        """
        pass
    
    @abstractmethod
    async def get_user_by_email(self, email: str) -> Optional[UserProfile]:
        """
        Retrieve user by email address.
        
        Args:
            email: User's email address
            
        Returns:
            UserProfile if found, None otherwise
            
        Note:
            Used for OAuth fallback when external_user_id lookup fails.
            Prevents duplicate user creation during mixed registration flows.
        """
        pass

    # ========================================================================
    # OAuth Multi-Tenant Session 2: OAuth identity methods
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # Purpose: Support OAuth provider identity lookup and platform linking
    # ========================================================================

    @abstractmethod
    async def get_user_by_external_id(self, external_user_id: str) -> Optional[UserProfile]:
        """
        Retrieve user by OAuth external identity.

        Args:
            external_user_id: OAuth identity with provider prefix (e.g., "firebase|abc123")

        Returns:
            UserProfile if found, None otherwise

        Note:
            Used by AuthenticationService after OAuth callback to find or register user.
            Requires Firestore index on external_user_id field for performance.

        Example:
            user = await repo.get_user_by_external_id("firebase|abc123")
        """
        pass

    @abstractmethod
    async def link_platform_identity(
        self,
        user_id: str,
        platform: str,
        platform_user_id: str
    ) -> UserProfile:
        """
        Link platform identity to existing user (Slack, Telegram, etc.).

        Args:
            user_id: Internal user UUID
            platform: Platform name ("slack", "telegram")
            platform_user_id: Platform-specific user ID

        Returns:
            Updated UserProfile with new platform identity

        Raises:
            ValueError: If user not found or identity already linked to another user

        Note:
            Updates UserProfile.platform_identities[platform] = platform_user_id
            Used when user signs in via OAuth, then connects Slack/Telegram

        Example:
            user = await repo.link_platform_identity(
                user_id="user-1",
                platform="slack",
                platform_user_id="U123456"
            )
        """
        pass
    
    @abstractmethod
    async def unlink_platform_identity(
        self,
        user_id: str,
        platform: str
    ) -> UserProfile:
        """
        Unlink platform identity from user.

        Args:
            user_id: Internal user UUID
            platform: Platform name ("slack", "telegram")

        Returns:
            Updated UserProfile with platform identity removed

        Raises:
            ValueError: If user not found

        Note:
            Removes UserProfile.platform_identities[platform]
            Used when user wants to disconnect Slack/Telegram

        Example:
            user = await repo.unlink_platform_identity(
                user_id="user-1",
                platform="slack"
            )
        """
        pass
    
    # Convenience aliases for Web UI (backward compatibility)
    async def add_platform_id(self, user_id: str, platform: str, platform_user_id: str) -> UserProfile:
        """Alias for link_platform_identity (Web UI compatibility)."""
        return await self.link_platform_identity(user_id, platform, platform_user_id)
    
    async def remove_platform_id(self, user_id: str, platform: str) -> UserProfile:
        """Alias for unlink_platform_identity (Web UI compatibility)."""
        return await self.unlink_platform_identity(user_id, platform)
    
    @abstractmethod
    async def create_user(self, user: UserProfile) -> UserProfile:
        """Create a new user profile."""
        pass
    
    @abstractmethod
    async def update_user(self, user: UserProfile) -> UserProfile:
        """Update existing user profile."""
        pass
    
    @abstractmethod
    async def delete_user(self, user_id: str) -> bool:
        """Hard delete user profile (GDPR 'Right to be Forgotten')."""
        pass

    @abstractmethod
    async def increment_usage(self, user_id: str, tokens: int, cost: float, requests: int = 1) -> None:
        """Atomically increment user usage and forward account-level increments."""
        pass
