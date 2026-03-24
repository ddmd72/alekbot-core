"""
LanguagePreferenceService — single write path for language preference changes.

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md §11

Writes to UserProfile (for UI language resolution) and to the prompt override
document (so the correct LANG_* token is picked up during assembly).
"""
from typing import TYPE_CHECKING, Optional, Tuple

from ..domain.language import LanguageCode
from ..ports.user_repository import UserRepository
from ..ports.account_repository import AccountRepository
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.language_service_port import LanguageServicePort
from ..ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from ..domain.prompt_v3.slot import OwnerType
from ..domain.prompt_v3.profile_slot import ProfileToken
from ..utils.logger import logger

if TYPE_CHECKING:
    from .user_notification_service import UserNotificationService

# All language token IDs — used to atomically swap one for another.
_ALL_LANG_TOKEN_IDS = frozenset({
    "LANG_MIRROR",
    "LANG_FIXED_UK",
    "LANG_FIXED_EN",
    "LANG_FIXED_FR",
    "LANG_FIXED_ES",
})

# Alert text injected into conversation when user changes language setting.
# Content mirrors the corresponding LANG_* token so the LLM sees the policy in context.
_LANG_ALERTS: dict = {
    "mirror": (
        "System Alert: User changed language settings in Cabinet.\n\n"
        "@critical\n"
        "rule Output_Language_Mirror() {\n"
        '    definition: "Dynamic output language policy. Mirrors the language of the user\'s input."\n'
        '    instruction: "Respond in the same language the user writes in. '
        "If they write in Ukrainian — respond in Ukrainian. "
        "If they switch to English — switch to English. "
        'Follow their language exactly, not a fixed rule."\n'
        "}"
    ),
    "uk": (
        "System Alert: User changed language settings in Cabinet.\n\n"
        "@critical\n"
        "rule Output_Language_Fixed_UK() {\n"
        '    definition: "Fixed output language policy. All responses in Ukrainian."\n'
        '    instruction: "Always respond in Ukrainian (uk), regardless of what language the user writes in."\n'
        '    negative_constraint: "Under NO circumstances output Russian text or Russian-specific '
        "characters ('ы', 'э', 'ъ', 'ё') as the final response.\"\n"
        "}"
    ),
    "en": (
        "System Alert: User changed language settings in Cabinet.\n\n"
        "@critical\n"
        "rule Output_Language_Fixed_EN() {\n"
        '    definition: "Fixed output language policy. All responses in English."\n'
        '    instruction: "Always respond in English, regardless of what language the user writes in."\n'
        "}"
    ),
    "fr": (
        "System Alert: User changed language settings in Cabinet.\n\n"
        "@critical\n"
        "rule Output_Language_Fixed_FR() {\n"
        '    definition: "Fixed output language policy. All responses in French."\n'
        '    instruction: "Always respond in French, regardless of what language the user writes in."\n'
        "}"
    ),
    "es": (
        "System Alert: User changed language settings in Cabinet.\n\n"
        "@critical\n"
        "rule Output_Language_Fixed_ES() {\n"
        '    definition: "Fixed output language policy. All responses in Spanish."\n'
        '    instruction: "Always respond in Spanish, regardless of what language the user writes in."\n'
        "}"
    ),
}


class LanguagePreferenceService(LanguageServicePort):
    """Single write path for language preference changes.

    Extension point: add side-effects here, callers never change.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        account_repo: AccountRepository,
        profile_repo: AgentProfileRepository,
        prompt_builder: PromptBuilderPort,
        system_default_language: LanguageCode = LanguageCode.EN,
        notification_service: Optional["UserNotificationService"] = None,
    ):
        self._user_repo = user_repo
        self._account_repo = account_repo
        self._profile_repo = profile_repo
        self._prompt_builder = prompt_builder
        self._system_default = system_default_language
        self._notification_service = notification_service
        self._ensure_agents = None  # Set after factory is created (main.py)

    async def set_preference(
        self,
        user_id: str,
        preferred_language: Optional[LanguageCode],
        agent_mirror: bool,
    ) -> None:
        """Update user's language preference.

        Writes preferred_language/agent_mirror to UserProfile (for UI localization)
        and upserts the corresponding LANG_* token into the USER override document
        (so prompt assembly picks up the correct language directive).
        """
        user = await self._user_repo.get_user(user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")

        user.config.preferred_language = preferred_language
        user.config.agent_mirror = agent_mirror
        await self._user_repo.update_user(user)

        # Update prompt override: swap language token atomically.
        if agent_mirror:
            # Mirror = default — clear any fixed-language override.
            # Agent profile already has LANG_MIRROR as its default slot.
            await self._profile_repo.set_override_tokens(
                OwnerType.USER, user_id, {},
                clear_ids=_ALL_LANG_TOKEN_IDS,
            )
        else:
            effective = preferred_language or self._system_default
            lang_token_id = f"LANG_FIXED_{effective.value.upper()}"
            await self._profile_repo.set_override_tokens(
                OwnerType.USER, user_id,
                {lang_token_id: ProfileToken(token_id=lang_token_id, order=70, non_overridable=False)},
                clear_ids=_ALL_LANG_TOKEN_IDS - {lang_token_id},
            )

        logger.info(
            f"Language preference updated: user={user_id} "
            f"preferred={preferred_language} mirror={agent_mirror}"
        )
        self._prompt_builder.invalidate_cache()

        if self._notification_service:
            alert_key = "mirror" if agent_mirror else (preferred_language or self._system_default).value
            alert_text = _LANG_ALERTS.get(alert_key)
            if alert_text:
                account_id = user.account_id or user_id
                try:
                    if self._ensure_agents:
                        await self._ensure_agents(user_id)
                    await self._notification_service.notify(
                        user_id=user_id,
                        account_id=account_id,
                        system_alert=alert_text,
                    )
                except Exception as exc:
                    logger.warning(f"Language change notification failed for {user_id[:8]}: {exc}")

    async def get_preference(self, user_id: str) -> Tuple[Optional[LanguageCode], bool]:
        """Returns (preferred_language, agent_mirror). Defaults: (None, True)."""
        user = await self._user_repo.get_user(user_id)
        if not user:
            return None, True
        return user.config.preferred_language, user.config.agent_mirror

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        """Resolve effective UI language for a user.

        Chain: USER preferred_language → ACCOUNT default_language → SYSTEM default.
        Called once per request by platform adapters.
        """
        user = await self._user_repo.get_user(user_id)
        if user and user.config.preferred_language:
            return user.config.preferred_language

        if user and user.account_id:
            account = await self._account_repo.get_account(user.account_id)
            if account and account.default_language:
                return account.default_language

        return self._system_default
