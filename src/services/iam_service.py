"""
IAM (Identity & Access Management) Service.

Centralized authorization logic for the system.
Replaces IdentityResolver with clean, testable IAM-centric architecture.
"""
from typing import Optional

from ..domain.user import UserProfile
from ..domain.billing import BillingAccount
from ..ports.user_repository import UserRepository
from ..ports.account_repository import AccountRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..ports.platform_auth_port import IAMDecision, PlatformAuthPort
from ..utils.logger import logger


class IAMService(PlatformAuthPort):
    """
    Centralized Identity & Access Management service.
    
    Single source of truth for authorization decisions.
    Replaces scattered authentication/authorization logic across adapters.
    
    Architecture:
    - Pure service layer (no adapter/infrastructure dependencies)
    - Uses Ports (repositories) for data access
    - Called by ALL adapters (Slack, OAuth, Telegram, etc)
    
    Decision Logic:
    1. Platform user EXISTS? → ALLOW
    2. OAuth + email EXISTS? → ALLOW  
    3. OAuth + email in WHITELIST? → CREATE
    4. DEFAULT → REJECT
    
    Key Principle:
    "Registration ONLY via Web UI (OAuth). Chat bots CANNOT create accounts."
    
    Message Generation:
    ALL user-facing messages are centralized here for:
    - Consistency across platforms
    - Easy localization
    - Testability
    - Single source of truth
    """
    
    # Centralized URLs
    CABINET_URL = "https://my.alekbot.app/cabinet"
    
    def __init__(
        self,
        user_repo: UserRepository,
        account_repo: AccountRepository,
        whitelist_repo: WhitelistRepository
    ):
        """
        Initialize IAMService with repository dependencies.
        
        Args:
            user_repo: User data repository
            account_repo: Account data repository
            whitelist_repo: Whitelist configuration repository
        """
        self.user_repo = user_repo
        self.account_repo = account_repo
        self.whitelist_repo = whitelist_repo
    
    def get_rejection_message(
        self,
        platform: str,
        platform_user_id: Optional[str] = None,
        reason: str = "not_registered"
    ) -> str:
        """
        Generate platform-specific rejection message.
        
        Centralizes all user-facing messages for:
        - Consistency across platforms
        - Easy localization (future: i18n support)
        - Testability
        
        Args:
            platform: Platform name ("slack", "telegram", etc)
            platform_user_id: Platform-specific user ID (for display in instructions)
            reason: Rejection reason ("not_registered", "revoked", etc)
            
        Returns:
            Localized, platform-appropriate rejection message
        """
        if reason == "not_registered":
            if platform == "telegram":
                # Ukrainian for Telegram (primary audience)
                msg = (
                    f"👋 Привіт! Щоб використовувати бота, потрібно зареєструватися.\n\n"
                    f"**Крок 1:** Відкрий {self.CABINET_URL}\n"
                    f"**Крок 2:** Авторизуйся через Google\n"
                    f"**Крок 3:** Підключи Telegram"
                )
                
                if platform_user_id:
                    msg += f" (ID: `{platform_user_id}`)"
                
                msg += "\n\n🔙 Після цього повернись сюди і напиши будь-що!"
                return msg
                
            elif platform == "slack":
                # English for Slack (international audience)
                return (
                    f"👋 Hi! To use the bot, please register first.\n\n"
                    f"**Step 1:** Open {self.CABINET_URL}\n"
                    f"**Step 2:** Sign in with Google\n"
                    f"**Step 3:** Link your Slack account\n\n"
                    f"🔙 Then come back here and send a message!"
                )
            else:
                # Generic fallback
                return (
                    f"👋 Account not found.\n\n"
                    f"Please register first:\n"
                    f"🔗 {self.CABINET_URL}"
                )
        
        elif reason == "revoked":
            # Same for all platforms
            return (
                "⛔ Your access has been revoked.\n\n"
                "Please contact the administrator."
            )
        
        else:
            # Fallback
            return "Authorization failed. Please contact support."
    
    async def authorize(
        self,
        platform: str,
        platform_user_id: Optional[str] = None,
        email: Optional[str] = None
    ) -> IAMDecision:
        """
        Make authorization decision for user access.
        
        Called by ALL platform adapters (Slack, OAuth, Telegram) at EVERY message.
        No caching for MVP - always checks fresh from database.
        
        Args:
            platform: Platform name ("slack", "telegram", "oauth", etc)
            platform_user_id: Platform-specific user ID (verified by platform API)
            email: Email address (only for OAuth)
            
        Returns:
            IAMDecision with action and user data
            
        Decision Tree:
            Branch 1: Platform user exists? → ALLOW (registered user)
            Branch 2: OAuth + email exists? → ALLOW (existing OAuth user)
            Branch 3: OAuth + whitelist? → CREATE (new registration)
            Default: REJECT (not authorized)
            
        Example:
            >>> # Slack user (registered)
            >>> decision = await iam.authorize("slack", platform_user_id="U123")
            >>> assert decision.action == "allow"
            >>> assert decision.user.user_id == "user_abc"
            
            >>> # Slack user (NOT registered)
            >>> decision = await iam.authorize("slack", platform_user_id="U999")
            >>> assert decision.action == "reject"
            >>> assert "Register" in decision.message
            
            >>> # OAuth user (whitelisted)
            >>> decision = await iam.authorize("oauth", email="admin@company.com")
            >>> assert decision.action == "create_account"
            
            >>> # OAuth user (NOT whitelisted)
            >>> decision = await iam.authorize("oauth", email="random@spam.com")
            >>> assert decision.action == "reject"
        """
        
        # ================================================================
        # BRANCH 1: Existing Platform User (Slack, Telegram, iOS)
        # ================================================================
        # Use case: Registered user returns to chat bot
        if platform_user_id and platform != "oauth":
            user = await self.user_repo.get_user_by_platform_id(
                platform,
                platform_user_id
            )
            
            if user:
                # Data integrity check - fail fast if email missing
                if not user.email:
                    raise ValueError(
                        f"[IAM] DATA CORRUPTION: User {user.user_id} has no email! "
                        f"Platform: {platform}, Platform ID: {platform_user_id}. "
                        f"Fix: Add email to user record in Firestore."
                    )
                
                # NEW (2026-02-05): Whitelist enforcement for ALL users
                # Can revoke access by removing email from whitelist
                whitelist = await self.whitelist_repo.get_whitelist()
                
                if not whitelist.is_allowed(user.email):
                    logger.warning(
                        f"⛔ [IAM] Access revoked for user {user.user_id}: {user.email}"
                    )
                    return IAMDecision(
                        action="reject",
                        message=self.get_rejection_message(
                            platform=platform,
                            reason="revoked"
                        )
                    )
                
                # Whitelist passed
                logger.info(
                    f"✅ [IAM] Authorized platform user: "
                    f"{platform}:{platform_user_id} → {user.user_id}"
                )
                return IAMDecision(action="allow", user=user)
            
            # User NOT found → REJECT (no auto-creation)
            logger.warning(
                f"⛔ [IAM] Unknown platform user: {platform}:{platform_user_id}"
            )
            return IAMDecision(
                action="reject",
                message=self.get_rejection_message(
                    platform=platform,
                    platform_user_id=platform_user_id,
                    reason="not_registered"
                ),
                metadata={"platform_user_id": platform_user_id}
            )
        
        # ================================================================
        # BRANCH 2 & 3: OAuth Authentication
        # ================================================================
        # Use case: User logs in via Google OAuth
        if platform == "oauth" and email:
            # 2a. Existing OAuth user?
            user = await self.user_repo.get_user_by_email(email)
            
            if user:
                logger.info(
                    f"✅ [IAM] Authorized OAuth user: {email} → {user.user_id}"
                )
                return IAMDecision(action="allow", user=user)
            
            # 2b. New user → Check whitelist
            whitelist = await self.whitelist_repo.get_whitelist()
            
            if not whitelist.is_allowed(email):
                logger.warning(f"⛔ [IAM] Email not in whitelist: {email}")
                return IAMDecision(
                    action="reject",
                    message=(
                        "Email not authorized. "
                        "Contact admin for access."
                    )
                )
            
            # 2c. Create new user (whitelist passed)
            logger.info(f"🆕 [IAM] Creating new user: {email}")
            user = await self._create_new_user(email)
            
            return IAMDecision(action="create_account", user=user)
        
        # ================================================================
        # DEFAULT: REJECT (Invalid parameters)
        # ================================================================
        logger.error(
            f"⛔ [IAM] Invalid authorize call: "
            f"platform={platform}, user_id={platform_user_id}, email={email}"
        )
        return IAMDecision(
            action="reject",
            message="Authorization failed. Invalid parameters."
        )
    
    async def _create_new_user(self, email: str) -> UserProfile:
        """
        Create new user with solo account (Master Account First paradigm).
        
        Args:
            email: Email address of new user
            
        Returns:
            Created UserProfile
            
        Note:
            This is ONLY called after whitelist check passed.
            Creates both UserProfile and solo BillingAccount.
        """
        # Create user profile
        new_user = UserProfile(
            email=email,
            external_user_id=f"firebase|{email}",  # Placeholder for Firebase UID
            display_name=email.split("@")[0]  # Use email prefix as display name
        )
        
        # Create solo billing account (user is OWNER)
        account = BillingAccount(
            owner_user_id=new_user.user_id,
            iam_policy={new_user.user_id: "OWNER"}  # User is owner of their account
        )
        
        await self.account_repo.create_account(account)
        logger.info(f"📋 [IAM] Created solo account: {account.account_id}")
        
        # Link user to account
        new_user.account_id = account.account_id
        created_user = await self.user_repo.create_user(new_user)
        
        logger.info(
            f"✅ [IAM] User created: {created_user.user_id} "
            f"(email={email}, account={account.account_id})"
        )
        
        return created_user
