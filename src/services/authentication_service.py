"""
Authentication Service (OAuth Multi-Tenant Session 4).

Handles OAuth authentication flows, user registration, and account creation.
Orchestrates AuthPort adapters, UserRepository, AccountRepository.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
from __future__ import annotations

from typing import Optional, Tuple, TYPE_CHECKING
from datetime import datetime, timezone
from uuid import uuid4

from ..ports.auth_port import AuthPort, TokenClaims, OAuthTokens, OAuthUserInfo
from ..ports.user_repository import UserRepository
from ..ports.account_repository import AccountRepository
from ..domain.user import UserProfile, UserBotConfig
from ..domain.billing import BillingAccount, AccountTier
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..services.auth_provider_registry import AuthProviderRegistry


class AuthenticationService:
    """
    OAuth authentication service.

    Handles the complete OAuth flow:
    1. User authorizes via OAuth provider (Firebase/Google)
    2. Backend receives authorization code
    3. Exchange code for tokens
    4. Verify ID token and extract user claims
    5. Find or create user account
    6. Return authenticated user

    Master Account First Paradigm:
    - First user registration creates a new BillingAccount (tenant)
    - User is assigned OWNER role in the account
    - Subsequent users can be invited to existing accounts
    """

    def __init__(
        self,
        auth_registry: AuthProviderRegistry,
        user_repo: UserRepository,
        account_repo: AccountRepository,
    ):
        """
        Initialize authentication service.

        Args:
            auth_registry: OAuth provider registry
            user_repo: User repository for CRUD operations
            account_repo: Account repository for billing/IAM
        """
        self.auth_registry = auth_registry
        self.user_repo = user_repo
        self.account_repo = account_repo

    async def handle_oauth_callback(
        self,
        code: str,
        redirect_uri: str,
        provider_name: Optional[str] = None,
    ) -> Tuple[UserProfile, BillingAccount, OAuthTokens]:
        """
        Handle OAuth callback after user authorization.

        Flow:
        1. Exchange authorization code for tokens
        2. Verify ID token
        3. Get user info from provider
        4. Find existing user or register new user
        5. Return authenticated user + account + tokens

        Args:
            code: Authorization code from OAuth provider
            redirect_uri: OAuth callback URL (must match authorization request)
            provider_name: OAuth provider name (default: from config)

        Returns:
            Tuple of (UserProfile, BillingAccount, OAuthTokens)

        Raises:
            ValueError: Invalid code, token verification failed, or network error
        """
        # Get OAuth provider
        auth_provider = self.auth_registry.get_provider(provider_name)
        provider_id = auth_provider.get_provider_name()

        logger.info(f"🔐 OAuth callback - provider: {provider_id}")

        # Step 1: Exchange code for tokens
        try:
            tokens = await auth_provider.exchange_code_for_tokens(code, redirect_uri)
            logger.info("✅ Authorization code exchanged for tokens")
        except Exception as e:
            logger.error(f"❌ Token exchange failed: {e}")
            raise ValueError(f"Failed to exchange authorization code: {e}")

        # Step 2: Verify ID token
        try:
            claims = await auth_provider.verify_token(tokens.id_token)
            logger.info(f"✅ ID token verified - sub: {claims.sub}")
        except Exception as e:
            logger.error(f"❌ Token verification failed: {e}")
            raise ValueError(f"Failed to verify ID token: {e}")

        # Step 3: Get user info from provider
        try:
            user_info = await auth_provider.get_user_info(tokens.access_token)
            logger.info(f"✅ User info retrieved - email: {user_info.email}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch user info (non-critical): {e}")
            # Fallback: use claims data
            user_info = OAuthUserInfo(
                sub=claims.sub,
                email=claims.email,
                name=claims.name,
                picture=claims.picture,
            )

        # Step 4: Find or register user
        external_user_id = f"{provider_id}|{claims.sub}"
        existing_user = await self.user_repo.get_user_by_external_id(external_user_id)

        if existing_user:
            logger.info(f"👤 Existing user found by external_id: {existing_user.user_id}")
            user = existing_user

            # Update auth metadata with latest provider data
            user.auth_metadata = {
                "email": user_info.email,
                "name": user_info.name,
                "picture": user_info.picture,
                "locale": user_info.locale,
                "email_verified": user_info.email_verified,
                "last_login": datetime.now(timezone.utc).isoformat(),
            }
            logger.debug(f"🔄 Updating user metadata: {user.user_id}")
            try:
                user = await self.user_repo.update_user(user)
            except Exception as e:
                logger.error(f"❌ Failed to update user: {e}", exc_info=True)
                raise
        else:
            # Fallback: Check by email (prevents duplicates for mixed registration)
            email = user_info.email or claims.email
            if email:
                logger.debug(f"🔍 Checking for existing user by email: {email}")
                existing_user_by_email = await self.user_repo.get_user_by_email(email)
                
                if existing_user_by_email:
                    logger.info(
                        f"👤 Found existing user by email (linking OAuth): "
                        f"{existing_user_by_email.user_id}"
                    )
                    
                    # Link OAuth identity to existing user
                    user = existing_user_by_email
                    user.external_user_id = external_user_id
                    user.auth_metadata = {
                        "email": user_info.email,
                        "name": user_info.name,
                        "picture": user_info.picture,
                        "locale": user_info.locale,
                        "email_verified": user_info.email_verified,
                        "provider": provider_id,
                        "linked_at": datetime.now(timezone.utc).isoformat(),
                    }
                    
                    try:
                        user = await self.user_repo.update_user(user)
                        logger.info(f"✅ OAuth linked to existing user: {user.user_id}")
                    except Exception as e:
                        logger.error(f"❌ Failed to link OAuth: {e}", exc_info=True)
                        raise
                else:
                    # No existing user - register new
                    logger.info("🆕 New user - registering")
                    user = await self.register_new_user(
                        external_user_id=external_user_id,
                        user_info=user_info,
                        claims=claims,
                    )
            else:
                # No email available - register new (edge case)
                logger.warning("⚠️ No email in OAuth claims, registering without email check")
                user = await self.register_new_user(
                    external_user_id=external_user_id,
                    user_info=user_info,
                    claims=claims,
                )

        # Step 5: Load billing account
        if not user.account_id:
            raise ValueError(f"User {user.user_id} has no account_id")

        logger.debug(f"🔍 Loading account: {user.account_id}")
        try:
            account = await self.account_repo.get_account(user.account_id)
        except Exception as e:
            logger.error(f"❌ Failed to load account: {e}", exc_info=True)
            raise

        if not account:
            raise ValueError(f"Account {user.account_id} not found")

        logger.info(
            f"✅ OAuth flow complete - user: {user.user_id}, "
            f"account: {account.account_id}, tier: {account.tier.value}"
        )

        return user, account, tokens

    async def register_new_user(
        self,
        external_user_id: str,
        user_info: OAuthUserInfo,
        claims: TokenClaims,
    ) -> UserProfile:
        """
        Register new user and create billing account (Master Account First).

        Flow:
        1. Create new BillingAccount (tenant)
        2. Create UserProfile linked to account
        3. Assign OWNER role in account's IAM policy
        4. Initialize account defaults (UserBotConfig)

        Args:
            external_user_id: OAuth identity ("firebase|abc123")
            user_info: User profile from OAuth provider
            claims: JWT claims from ID token

        Returns:
            Newly created UserProfile

        Note:
            This implements Master Account First paradigm:
            - Every new user gets their own BillingAccount
            - User is the OWNER of their account
            - Future: support invitations to existing accounts
        """
        logger.info(f"🆕 Registering new user: {external_user_id}")

        # Step 1: Create billing account (tenant)
        account = BillingAccount(
            account_id=f"account-{uuid4()}",
            tier=AccountTier.FREE,  # Default tier for new accounts
            iam_policy={},  # Will be populated with OWNER role below
            account_defaults=UserBotConfig(),  # Default bot config
        )

        # Step 2: Create user profile
        user = UserProfile(
            user_id=str(uuid4()),
            external_user_id=external_user_id,
            email=user_info.email or claims.email,
            display_name=user_info.name or claims.name or "Anonymous",
            auth_metadata={
                "email": user_info.email,
                "name": user_info.name,
                "picture": user_info.picture,
                "locale": user_info.locale,
                "email_verified": user_info.email_verified,
                "provider": external_user_id.split("|")[0],
                "registered_at": datetime.now(timezone.utc).isoformat(),
            },
            platform_identities={},  # Empty - will be linked later (Slack, Telegram)
            account_id=account.account_id,
            config=UserBotConfig(),  # User-specific config (overrides account defaults)
        )

        # Step 3: Assign OWNER role in IAM policy
        account.iam_policy[user.user_id] = "owner"

        # Step 4: Persist to database
        try:
            # Create account first (user FK references account)
            account = await self.account_repo.create_account(account)
            logger.info(f"✅ Created account: {account.account_id}")

            # Create user
            user = await self.user_repo.create_user(user)
            logger.info(f"✅ Created user: {user.user_id} ({user.display_name})")

            return user
        except Exception as e:
            logger.error(f"❌ Failed to register user: {e}")
            # TODO: Rollback account creation if user creation fails
            raise ValueError(f"User registration failed: {e}")

    async def link_platform_identity(
        self,
        user_id: str,
        platform: str,
        platform_user_id: str,
    ) -> UserProfile:
        """
        Link platform identity to existing user (Slack, Telegram, etc.).

        Used when:
        - User signs in via OAuth, then connects Slack
        - User signs in via OAuth, then connects Telegram

        Args:
            user_id: Internal user UUID
            platform: Platform name ("slack", "telegram")
            platform_user_id: Platform-specific user ID

        Returns:
            Updated UserProfile with new platform identity

        Raises:
            ValueError: If user not found or identity already linked to another user
        """
        logger.info(f"🔗 Linking platform identity: {platform}={platform_user_id} to user={user_id}")

        user = await self.user_repo.link_platform_identity(
            user_id=user_id,
            platform=platform,
            platform_user_id=platform_user_id,
        )

        logger.info(f"✅ Platform identity linked: {platform}")
        return user

    async def get_user_by_external_id(self, external_user_id: str) -> Optional[UserProfile]:
        """
        Get user by OAuth external identity.

        Args:
            external_user_id: OAuth identity ("firebase|abc123")

        Returns:
            UserProfile if found, None otherwise
        """
        return await self.user_repo.get_user_by_external_id(external_user_id)

    async def get_user_by_platform_id(
        self,
        platform: str,
        platform_user_id: str,
    ) -> Optional[UserProfile]:
        """
        Get user by platform identity (Slack, Telegram).

        Args:
            platform: Platform name ("slack", "telegram")
            platform_user_id: Platform-specific user ID

        Returns:
            UserProfile if found, None otherwise
        """
        return await self.user_repo.get_user_by_platform_id(platform, platform_user_id)

    async def link_oauth_identity(
        self,
        user_id: str,
        code: str,
        redirect_uri: str,
        provider_name: Optional[str] = None,
    ) -> UserProfile:
        """
        Link OAuth identity to existing user.

        Use case: User already exists (registered via Slack/Telegram),
        wants to add Google OAuth for web UI access.

        Flow:
        1. Exchange OAuth code for tokens
        2. Verify ID token → get external_user_id
        3. Check if external_user_id already linked to another user
        4. Link external_user_id to current user
        5. Return updated user

        Args:
            user_id: Existing user UUID
            code: OAuth authorization code
            redirect_uri: OAuth callback URL
            provider_name: OAuth provider name (default: from config)

        Returns:
            Updated UserProfile with external_user_id

        Raises:
            ValueError: If user not found, OAuth identity already linked, or exchange failed
        """
        logger.info(f"🔗 Linking OAuth identity to existing user: {user_id}")

        # Get OAuth provider
        auth_provider = self.auth_registry.get_provider(provider_name)
        provider_id = auth_provider.get_provider_name()

        # Step 1: Exchange code for tokens
        try:
            tokens = await auth_provider.exchange_code_for_tokens(code, redirect_uri)
            logger.info("✅ Authorization code exchanged for tokens")
        except Exception as e:
            logger.error(f"❌ Token exchange failed: {e}")
            raise ValueError(f"Failed to exchange authorization code: {e}")

        # Step 2: Verify ID token
        try:
            claims = await auth_provider.verify_token(tokens.id_token)
            logger.info(f"✅ ID token verified - sub: {claims.sub}")
        except Exception as e:
            logger.error(f"❌ Token verification failed: {e}")
            raise ValueError(f"Failed to verify ID token: {e}")

        # Step 3: Get user info (optional, for metadata)
        try:
            user_info = await auth_provider.get_user_info(tokens.access_token)
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch user info (non-critical): {e}")
            user_info = OAuthUserInfo(
                sub=claims.sub,
                email=claims.email,
                name=claims.name,
                picture=claims.picture,
            )

        # Step 4: Check if external_user_id already linked
        external_user_id = f"{provider_id}|{claims.sub}"
        existing_user = await self.user_repo.get_user_by_external_id(external_user_id)

        if existing_user and existing_user.user_id != user_id:
            raise ValueError(
                f"OAuth identity {external_user_id} already linked to user {existing_user.user_id}"
            )

        # Step 5: Get current user and link OAuth identity
        user = await self.user_repo.get_user(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        # Update user with OAuth identity
        user.external_user_id = external_user_id
        user.auth_metadata = {
            "email": user_info.email,
            "name": user_info.name,
            "picture": user_info.picture,
            "locale": user_info.locale,
            "email_verified": user_info.email_verified,
            "provider": provider_id,
            "linked_at": datetime.now(timezone.utc).isoformat(),
        }

        user = await self.user_repo.update_user(user)

        logger.info(f"✅ OAuth identity linked: {external_user_id} → user {user_id}")
        return user
