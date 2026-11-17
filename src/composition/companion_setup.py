"""The companions' memory (COMPANION_AGENTS_RFC): the context assembler the agent factory hands every companion, the
extractor that turns a companion channel's overflow into companion memory, and the web call's "keep as a file"
(VOICE_CALL_LIVE_TEXT_RFC §4.11), which runs the same extractor."""
from dataclasses import dataclass
from typing import Any

from ..adapters.firestore_companion_cache_repository import FirestoreCompanionCacheRepository
from ..adapters.firestore_companion_extraction_queue import FirestoreCompanionExtractionQueue
from ..adapters.firestore_companion_memory_repository import FirestoreCompanionMemoryRepository
from ..config.environment import EnvironmentConfig
from ..domain.settings import SearchConfig
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..services.call_text_file_service import CallTextFileService
from ..services.companion_context_assembler_service import CompanionContextAssemblerService
from ..services.companion_extraction_service import CompanionExtractionService
from ..services.prompt_builder import PromptBuilder
from ..services.search_enrichment_service import SearchEnrichmentService
from ..services.user_notification_service import UserNotificationService
from ..utils.logger import logger
from .companion_extractor_runner import CompanionExtractorRunner
from .language_setup import Languages

_COMPANION_SEARCH_TOTAL = 10


@dataclass(frozen=True)
class Companions:
    context_assembler: CompanionContextAssemblerService
    extractor_runner: CompanionExtractorRunner
    extraction_service: CompanionExtractionService
    call_text_files: CallTextFileService


def build_companions(db_client: Any, env_config: EnvironmentConfig, *, container: Any, user_repo: UserRepository,
                     quota_service: QuotaService, extraction_queue: FirestoreCompanionExtractionQueue,
                     notification_service: UserNotificationService, languages: Languages) -> Companions:
    """One companion memory store and one cache shared by the assembler and the extraction service; the fact search of
    the assembler at the system defaults with its own total."""
    logger.info("🧑‍🏫 Initializing companion memory: context, extraction, call text files...")
    memory_repo = FirestoreCompanionMemoryRepository(db_client=db_client, env_config=env_config)
    cache_repo = FirestoreCompanionCacheRepository(db_client=db_client, env_config=env_config)
    search = SearchConfig()
    enrichment = SearchEnrichmentService(
        repository=container.repository,
        embedding_service=container.embedding_service,
        keyword_limit=search.DEFAULT_KEYWORD_LIMIT,
        phrase_one_limit=search.DEFAULT_PHRASE_ONE_LIMIT,
        phrase_two_limit=search.DEFAULT_PHRASE_TWO_LIMIT,
        total_limit=_COMPANION_SEARCH_TOTAL,
    )
    assembler = CompanionContextAssemblerService(
        companion_repo=memory_repo,
        cache_repo=cache_repo,
        embedding_service=container.embedding_service,
        enrichment_port=enrichment,
        fact_repo=container.repository,
    )
    # repo=None is safe: the tutor extractor builds its prompt with include_biographical=False, the only path that reads it.
    extractor = CompanionExtractorRunner(
        context_builder=container.context_builder,
        user_repo=user_repo,
        prompt_builder=PromptBuilder(repo=None, assembly_service=container.assembly_service),
        quota_service=quota_service,
        prompt_content_store=container.prompt_content_store,
        language_service=languages.resolver,
    )
    call_text_files = CallTextFileService(
        extractor=extractor,
        drive=container.user_drive_service,
        notifications=notification_service,
        users=user_repo,
        localization=languages.localization_port,
        language=languages.resolver,
        prompt_content=container.prompt_content_store,
    )
    extraction = CompanionExtractionService(
        queue=extraction_queue,
        companion_repo=memory_repo,
        cache_repo=cache_repo,
        embedding_service=container.embedding_service,
        extractor=extractor,
        quota=quota_service,
    )
    return Companions(context_assembler=assembler, extractor_runner=extractor, extraction_service=extraction,
                      call_text_files=call_text_files)
