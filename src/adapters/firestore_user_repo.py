from typing import Optional
from datetime import datetime, timezone
import asyncio
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from ..domain.user import UserProfile
from ..ports.user_repository import UserRepository
from ..ports.account_repository import AccountRepository
from ..config.environment import EnvironmentConfig
from ..utils.logger import logger
from ..utils.timer import log_execution_time

class FirestoreUserRepository(UserRepository):
    """
    Firestore implementation of UserRepository.
    Stores users in '{env}_users' collection.
    """
    
    def __init__(self, db_client, env_config: EnvironmentConfig, account_repo: AccountRepository):
        self.db = db_client
        self.env_config = env_config
        self.account_repo = account_repo

        # Use dynamic collection name (ADR-006 Semantic Naming)
        collection_name = env_config.domain_users_collection
        self.users_col = self.db.collection(collection_name)

        logger.info(f"📂 User Repository initialized. Collection: {collection_name}")

    @log_execution_time
    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        doc = await self.users_col.document(user_id).get()
        if doc.exists:
            return UserProfile(**doc.to_dict())
        return None

    @log_execution_time
    async def get_user_by_platform_id(self, platform: str, platform_user_id: str) -> Optional[UserProfile]:
        """
        Find user by platform identity using Firestore query.
        Query: where(f"platform_identities.{platform}", "==", platform_user_id)
        """
        # Note: This requires a composite index if we query by multiple fields,
        # but for single field equality it works out of the box.
        field_path = f"platform_identities.{platform}"

        query = self.users_col.where(filter=FieldFilter(field_path, "==", platform_user_id)).limit(1)
        docs = query.stream()

        async for doc in docs:
            return UserProfile(**doc.to_dict())

        return None
    
    @log_execution_time
    async def get_user_by_email(self, email: str) -> Optional[UserProfile]:
        """
        Find user by email address.
        Query: where("email", "==", email)
        
        Note:
            Used for OAuth fallback when external_user_id lookup fails.
            Prevents duplicate user creation during mixed registration flows.
        """
        query = self.users_col.where(filter=FieldFilter("email", "==", email)).limit(1)
        docs = query.stream()

        async for doc in docs:
            logger.debug(f"🔍 Found user by email: {email} → {doc.id}")
            return UserProfile(**doc.to_dict())

        logger.debug(f"🔍 No user found for email: {email}")
        return None

    @log_execution_time
    async def get_user_by_external_id(self, external_user_id: str) -> Optional[UserProfile]:
        """
        Find user by OAuth external identity.

        OAuth Multi-Tenant Session 7: Repository implementation.
        Query: where("external_user_id", "==", external_user_id)

        Note: Requires Firestore index on external_user_id field for performance.
        Index will be created automatically on first query.

        Args:
            external_user_id: OAuth identity with provider prefix (e.g., "firebase|abc123")

        Returns:
            UserProfile if found, None otherwise
        """
        query = self.users_col.where(
            filter=FieldFilter("external_user_id", "==", external_user_id)
        ).limit(1)
        docs = query.stream()

        async for doc in docs:
            logger.debug(f"🔍 Found user by external_id: {external_user_id} → {doc.id}")
            return UserProfile(**doc.to_dict())

        logger.debug(f"🔍 No user found for external_id: {external_user_id}")
        return None

    @log_execution_time
    async def link_platform_identity(
        self,
        user_id: str,
        platform: str,
        platform_user_id: str
    ) -> UserProfile:
        """
        Link platform identity to existing user (Slack, Telegram, etc.).

        OAuth Multi-Tenant Session 7: Repository implementation.

        Args:
            user_id: Internal user UUID
            platform: Platform name ("slack", "telegram")
            platform_user_id: Platform-specific user ID

        Returns:
            Updated UserProfile with new platform identity

        Raises:
            ValueError: If user not found or identity already linked to another user
        """
        # Check if user exists
        user = await self.get_user(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        # Check if this platform identity is already linked to another user
        existing_user = await self.get_user_by_platform_id(platform, platform_user_id)
        if existing_user and existing_user.user_id != user_id:
            raise ValueError(
                f"Platform identity {platform}:{platform_user_id} already linked to user {existing_user.user_id}"
            )

        # Update user's platform_identities
        user.platform_identities[platform] = platform_user_id
        user.updated_at = datetime.now(timezone.utc)

        # Persist to Firestore
        await self.users_col.document(user_id).set(user.model_dump())

        logger.info(
            f"🔗 Linked platform identity: user {user_id} → {platform}:{platform_user_id}"
        )

        return user
    
    @log_execution_time
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
        """
        # Check if user exists
        user = await self.get_user(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        # Remove platform identity if exists
        if platform in user.platform_identities:
            del user.platform_identities[platform]
            user.updated_at = datetime.now(timezone.utc)

            # Persist to Firestore
            await self.users_col.document(user_id).set(user.model_dump())

            logger.info(
                f"🔓 Unlinked platform identity: user {user_id} → {platform}"
            )
        else:
            logger.debug(f"Platform {platform} not linked to user {user_id}, nothing to unlink")

        return user

    async def create_user(self, user: UserProfile) -> UserProfile:
        await self.users_col.document(user.user_id).set(user.model_dump())
        logger.info(f"👤 Created new user: {user.user_id} ({user.display_name})")
        return user

    async def update_user(self, user: UserProfile) -> UserProfile:
        user.updated_at = datetime.now(timezone.utc)
        await self.users_col.document(user.user_id).set(user.model_dump())
        return user

    async def delete_user(self, user_id: str) -> bool:
        # Production safety check
        if self.env_config.is_production:
            logger.warning(f"⚠️ Hard delete of user {user_id} requested in PRODUCTION")

        await self.users_col.document(user_id).delete()
        logger.info(f"🗑️ Deleted user: {user_id}")
        return True

    async def increment_usage(self, user_id: str, tokens: int, cost: float, requests: int = 1) -> None:
        """
        OAuth Multi-Tenant Session 8: Usage tracking moved to account level.
        User profile no longer has usage field - all billing is at account level.
        """
        user = await self.get_user(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        # OAuth refactor: Usage tracking moved to account level
        if user.account_id:
            await self.account_repo.increment_account_usage(
                account_id=user.account_id,
                tokens=tokens,
                cost=cost
            )
        else:
            logger.warning(f"User {user_id} has no account_id, cannot track usage")
