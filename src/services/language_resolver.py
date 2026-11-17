"""
LanguageResolver — the read side of a user's language settings (MULTILINGUAL_SUPPORT_RFC §11).

Reads only: the UI language (USER -> ACCOUNT -> SYSTEM) and the stored preference. Writes go through
`LanguagePreferenceService`, which takes this resolver, so the readers (platform adapters, the notification service,
the agent factory, the drive mirror) depend on nothing that notifies or refreshes agents.
"""
from typing import Optional, Tuple

from ..domain.language import LanguageCode
from ..ports.account_repository import AccountRepository
from ..ports.language_service_port import LanguageServicePort
from ..ports.user_repository import UserRepository


class LanguageResolver(LanguageServicePort):
    """Resolves a user's UI language and reads the stored language preference."""

    def __init__(
        self,
        user_repo: UserRepository,
        account_repo: AccountRepository,
        system_default_language: LanguageCode = LanguageCode.EN,
    ):
        self._user_repo = user_repo
        self._account_repo = account_repo
        self._system_default = system_default_language

    @property
    def system_default(self) -> LanguageCode:
        """The deployment's language, the last link of the resolution chain (`SYSTEM_DEFAULT_LANGUAGE`)."""
        return self._system_default

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
