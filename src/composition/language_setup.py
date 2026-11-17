"""The read side of language (decisions/composition_root_consumers.md §6): the UI strings and the resolver of a user's UI
language and stored preference. The writer, LanguagePreferenceService, is built with the agents (it refreshes them)."""
from dataclasses import dataclass

from ..adapters.file_localization_adapter import FileLocalizationAdapter
from ..domain.language import LanguageCode
from ..ports.account_repository import AccountRepository
from ..ports.user_repository import UserRepository
from ..services.language_resolver import LanguageResolver
from ..services.localization_service import LocalizationService
from ..utils.logger import logger


@dataclass(frozen=True)
class Languages:
    """`localization_port` is also read directly, by the memory review's question texts and the call's text files."""

    localization_port: FileLocalizationAdapter
    localization: LocalizationService
    resolver: LanguageResolver
    system_default: LanguageCode


def build_languages(config: dict, *, user_repo: UserRepository, account_repo: AccountRepository) -> Languages:
    """SYSTEM_DEFAULT_LANGUAGE (default English) is the language of a user with no preference."""
    logger.info("🌐 Initializing Language Services...")
    port = FileLocalizationAdapter()
    system_default = LanguageCode.from_str(config.get("SYSTEM_DEFAULT_LANGUAGE", "en"), default=LanguageCode.EN)
    return Languages(
        localization_port=port,
        localization=LocalizationService(port),
        resolver=LanguageResolver(user_repo=user_repo, account_repo=account_repo, system_default_language=system_default),
        system_default=system_default,
    )
