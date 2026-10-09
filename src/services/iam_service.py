"""
IAM (Identity & Access Management) Service.

Centralized authorization logic for the system.
Replaces IdentityResolver with clean, testable IAM-centric architecture.
"""
from typing import Optional

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
    - Called by the chat adapters (Slack, Telegram) on every message
    
    Decision Logic:
    1. Platform user EXISTS and email WHITELISTED? → ALLOW
    2. DEFAULT → REJECT
    
    Key Principle:
    "Registration ONLY via Web UI (OAuth). Chat bots CANNOT create accounts."
    The Web UI's own whitelist gate lives in AuthenticationService
    (checked at every OAuth sign-in), not here.
    
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
            # Pre-auth: the user's language preference does not exist before
            # registration, so onboarding is English (system default) on every platform.
            if platform == "telegram":
                msg = (
                    f"👋 Hi! To use the bot, please register first.\n\n"
                    f"**Step 1:** Open {self.CABINET_URL}\n"
                    f"**Step 2:** Sign in with Google\n"
                    f"**Step 3:** Link your Telegram account"
                )

                if platform_user_id:
                    msg += f" (ID: `{platform_user_id}`)"

                msg += "\n\n🔙 Then come back here and send a message!"
                return msg

            elif platform == "slack":
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
    ) -> IAMDecision:
        """
        Make authorization decision for a chat platform user.
        
        Called by the chat adapters (Slack, Telegram) at EVERY message.
        No caching for MVP - always checks fresh from database.
        
        Args:
            platform: Platform name ("slack", "telegram")
            platform_user_id: Platform-specific user ID (verified by platform API)
            
        Returns:
            IAMDecision with action and user data
            
        Decision Tree:
            Branch 1: Platform user exists and is whitelisted? → ALLOW
            Default: REJECT (not registered, revoked, or invalid parameters)
            
        Example:
            >>> # Slack user (registered)
            >>> decision = await iam.authorize("slack", platform_user_id="U123")
            >>> assert decision.action == "allow"
            >>> assert decision.user.user_id == "user_abc"
            
            >>> # Slack user (NOT registered)
            >>> decision = await iam.authorize("slack", platform_user_id="U999")
            >>> assert decision.action == "reject"
            >>> assert "Register" in decision.message
        """
        
        # ================================================================
        # BRANCH 1: Existing Platform User (Slack, Telegram, iOS)
        # ================================================================
        # Use case: Registered user returns to chat bot
        if platform_user_id:
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
        # DEFAULT: REJECT (Invalid parameters)
        # ================================================================
        logger.error(
            f"⛔ [IAM] Invalid authorize call: "
            f"platform={platform}, user_id={platform_user_id}"
        )
        return IAMDecision(
            action="reject",
            message="Authorization failed. Invalid parameters."
        )
