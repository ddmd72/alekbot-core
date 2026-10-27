from abc import ABC, abstractmethod
from typing import Optional
from ..domain.user import UserProfile

class UserRepository(ABC):
    """Port for user profiles (one record per user_id).

    Holds for every method:
    - Tenancy: user_id is global; lookups by platform identity, email or external id search all users.
    - Legacy: complexity overrides with an empty tier are dropped on read; unknown keys are ignored on read and dropped
      by the next create/update. A record that fails UserProfile validation otherwise: no writer produces one;
      behaviour pending the data audit.
    - Atomicity: create_user, update_user and unlink_platform_identity replace the whole record with no precondition;
      of two concurrent read-change-write updates of one user only the last survives (P11-G2-4).
      link_platform_identity checks the identity and writes in one atomic step.
    - Errors: backend errors propagate.
    """
    
    @abstractmethod
    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        """The profile, or None when missing."""
        pass
    
    @abstractmethod
    async def get_user_by_platform_id(self, platform: str, platform_user_id: str) -> Optional[UserProfile]:
        """The user whose platform_identities[platform] equals platform_user_id, or None. Used for identity
        resolution (one user per identity)."""
        pass
    
    @abstractmethod
    async def get_user_by_email(self, email: str) -> Optional[UserProfile]:
        """
        Retrieve user by email address.
        
        Args:
            email: User's email address
            
        Returns:
            UserProfile if found (exact match on the stored email), None otherwise

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
            ValueError: If user not found or identity already linked to another user (nothing is written);
            re-linking the same identity to the same user succeeds

        Note:
            Updates UserProfile.platform_identities[platform] = platform_user_id and updated_at
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
            Removes UserProfile.platform_identities[platform] (a no-op when absent); see Atomicity
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
        """Write the profile under user_id (replaces any existing record)."""
        pass
    
    @abstractmethod
    async def update_user(self, user: UserProfile) -> UserProfile:
        """Write the whole profile and set updated_at; see Atomicity."""
        pass
    
    @abstractmethod
    async def delete_user(self, user_id: str) -> bool:
        """Hard delete user profile (GDPR 'Right to be Forgotten'). No caller in src/."""
        pass

    @abstractmethod
    async def increment_usage(self, user_id: str, tokens: int, cost: float, requests: int = 1) -> None:
        """Forward the usage to the user's account (ValueError when the user is missing). No caller in src/."""
        pass
