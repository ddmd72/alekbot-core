"""
The drive indexer's model wiring (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.2, §24.9; D7, D12).

Builds, for `indexer_main.py` only: a ProviderRegistry with Claude and OpenAI (the primaries a user's configuration may
pick) and Gemini (the drive agents' fallback, D7 rev 3.17), an AgentContextBuilder over it with a process-local breaker, the
Firestore prompt builder, the quota service that bills each call to the user's account, and the runners: the reader
and the triage, and Search v2's profiler and passage hints (§25.4.4), all over one context builder (one breaker per
provider), one prompt builder and one quota service (`build_drive_models`). The extractor registry is
`{PARSER: parser, READER: reader runner}` (EXTRACTOR_CHAINS ids); the reader cuts PDFs with the same sandboxed
parser. `indexer_main.py` wires all four into DriveIndexingService (Search v2 P5); `build_drive_model_extractors`
keeps the v1 shape (extractors, triage) for the callers that need only those.

Deliberately absent: prompt caching (the prompts are below the providers' minimum cacheable size), the alerting
proxy (a scan the provider refuses is a per-file outcome, not a Slack alert), the prompt-content store (verbatim
scans stay out of BigQuery, §24.2), and Logfire. A provider without a key is not registered: a user whose
configuration names it gets a model outage (logged ERROR), parsing goes on; without GEMINI_API_KEY the agents run with
no fallback (a primary outage defers model work, §24.8).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from ..adapters.claude_adapter import ClaudeAdapter
from ..adapters.firestore_quota_service import FirestoreQuotaService
from ..adapters.drive_parser.media_extractor import SubprocessMediaExtractor
from ..adapters.gemini_adapter import GeminiAdapter
from ..adapters.gemini_batch_adapter import GeminiBatchAdapter
from ..adapters.in_memory_provider_resilience import InMemoryProviderResilience
from ..adapters.openai_adapter import OpenAIAdapter
from ..domain.drive_extraction import PARSER, READER
from ..domain.user import PerformanceTier
from ..ports.account_repository import AccountRepository
from ..ports.alert_sink import AlertSinkPort
from ..ports.content_extractor_port import ContentExtractorPort
from ..ports.document_profiler_port import DocumentProfilerPort
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.image_triage_port import ImageTriagePort
from ..ports.passage_hint_port import PassageHintPort
from ..ports.quota_service import QuotaService
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..services.agent_context_builder import AgentContextBuilder
from ..services.prompt_builder import PromptBuilder
from ..services.drive_transcript_stage import DriveTranscriptStage
from ..services.provider_registry import ProviderRegistry
from ..utils.logger import logger
from .drive_passage_runner import DrivePassageRunner
from .drive_profiler_runner import DriveProfilerRunner
from .drive_reader_runner import DriveReaderRunner, PdfSplitter
from .drive_transcript_runner import DriveTranscriptRunner
from .drive_triage_runner import DriveTriageRunner


class _DrivePromptBuilder(PromptBuilder):
    """The drive agents' prompt builder. Invalidating its whole cache also clears the assembled-prompt cache of its
    assembly service (24 h TTL), which on the indexer serves only the drive agents: build_drive_prompt invalidates
    an assembly it rejects, so it is rebuilt once the missing token or profile is uploaded."""

    def invalidate_cache(self, component_key: Optional[str] = None) -> None:
        super().invalidate_cache(component_key)
        if component_key is None and self.assembly_service is not None:
            self.assembly_service.invalidate_cache()


def build_drive_model_registry(keys: Mapping[str, Optional[str]]) -> ProviderRegistry:
    """Claude, OpenAI and Gemini, each only when its key is set. Gemini serves only the drive agents' fallback (Gemini
    Flash, `fallback_tier` in their strategy). Keys are stripped: a secret stored with a trailing newline becomes an
    illegal header that looks like a network error."""
    registry: ProviderRegistry = ProviderRegistry()
    anthropic_key = (keys.get("ANTHROPIC_API_KEY") or "").strip()
    openai_key = (keys.get("OPENAI_API_KEY") or "").strip()
    gemini_key = (keys.get("GEMINI_API_KEY") or "").strip()
    if anthropic_key:
        registry.register("claude", ClaudeAdapter(api_key=anthropic_key))
    if openai_key:
        registry.register("openai", OpenAIAdapter(api_key=openai_key))
    if gemini_key:
        registry.register("gemini", GeminiAdapter(api_key=gemini_key))
    if not gemini_key:
        logger.warning("[Indexer] GEMINI_API_KEY not set: the drive models run without their Gemini fallback")
    missing = [name for name, key in (("ANTHROPIC_API_KEY", anthropic_key), ("OPENAI_API_KEY", openai_key))
               if not key]
    if missing:
        logger.warning(f"[Indexer] {', '.join(missing)} not set: the drive models on that provider are unavailable")
    return registry


def build_drive_context_builder(keys: Mapping[str, Optional[str]]) -> AgentContextBuilder:
    """The indexer's context builder: the drive model registry with a process-local breaker, nothing else. The drive
    eval builds its runners on it too, so it measures the models the indexer calls."""
    return AgentContextBuilder(build_drive_model_registry(keys), resilience_port=InMemoryProviderResilience())


@dataclass(frozen=True)
class DriveModels:
    """Every drive model runner of the indexer, over one context builder, prompt builder and quota service."""
    extractors: Dict[str, ContentExtractorPort]
    triage: ImageTriagePort
    profiler: DocumentProfilerPort
    passages: PassageHintPort
    prompt_builder: Optional[PromptBuilder] = None   # the transcriber's runner is built over the same ones
    quota: Optional[QuotaService] = None


def build_drive_models(*, parser: Any, users: UserRepository, account_repo: AccountRepository,
                       assembly_service: Any, keys: Mapping[str, Optional[str]],
                       alert_sink: Optional[AlertSinkPort] = None) -> DriveModels:
    """The indexer's model runners. `parser` is the child-process parser (a ContentExtractorPort with
    `pdf_page_count` and `cut_pdf_window`)."""
    context_builder = build_drive_context_builder(keys)
    prompt_builder = _DrivePromptBuilder(repo=None, assembly_service=assembly_service)
    quota = FirestoreQuotaService(account_repo, alert_sink=alert_sink)
    splitter: PdfSplitter = parser
    reader = DriveReaderRunner(context_builder, users, prompt_builder, splitter, quota_service=quota)
    return DriveModels(
        extractors={PARSER: parser, READER: reader},
        triage=DriveTriageRunner(context_builder, users, prompt_builder, quota_service=quota),
        profiler=DriveProfilerRunner(context_builder, users, prompt_builder, quota_service=quota),
        passages=DrivePassageRunner(context_builder, users, prompt_builder, quota_service=quota),
        prompt_builder=prompt_builder, quota=quota)


def build_drive_transcription(*, enabled: bool, repo: DriveIndexRepository, drive: UserDrivePort, models: DriveModels,
                              keys: Mapping[str, Optional[str]], parser_user: Optional[str],
                              monthly_cap_usd: float) -> Optional[DriveTranscriptStage]:
    """The transcript stage (USER_DRIVE_INDEXING_RFC §27; plan Task 14B). Built ONLY while the kill switch
    (`DRIVE_TRANSCRIBE_ENABLED`) is on, so a deploy with it off leaves the indexer's object graph as it was; without
    GEMINI_API_KEY it is not built either (logged ERROR: the switch is on and nothing can transcribe). The batch adapter
    runs the BALANCED Gemini model (G26, never flash-lite, D44); the media extractor runs its children as the image's
    unprivileged parser user. `monthly_cap_usd` is `effective_monthly_cap(DRIVE_TRANSCRIBE_USD_CAP)`, decided once by
    the caller and shared with DriveIndexingService."""
    if not enabled:
        return None
    gemini_key = (keys.get("GEMINI_API_KEY") or "").strip()
    if not gemini_key:
        logger.error("[Indexer] DRIVE_TRANSCRIBE_ENABLED is on but GEMINI_API_KEY is not set: the transcript stage is not built")
        return None
    if models.prompt_builder is None or models.quota is None:
        logger.error("[Indexer] the drive models carry no prompt builder or quota service: the transcript stage is not built")
        return None
    batch = GeminiBatchAdapter(api_key=gemini_key, model=GeminiAdapter.MODEL_TIERS[PerformanceTier.BALANCED])
    runner = DriveTranscriptRunner(batch, models.prompt_builder, quota_service=models.quota)
    return DriveTranscriptStage(repo, drive, SubprocessMediaExtractor(sandbox_user=parser_user), batch, runner,
                                monthly_cap_usd=monthly_cap_usd)


def build_drive_model_extractors(*, parser: Any, users: UserRepository, account_repo: AccountRepository,
                                 assembly_service: Any, keys: Mapping[str, Optional[str]],
                                 alert_sink: Optional[AlertSinkPort] = None,
                                 ) -> Tuple[Dict[str, ContentExtractorPort], ImageTriagePort]:
    """The extractor registry and the triage port for DriveIndexingService (the v1 shape of `build_drive_models`)."""
    models = build_drive_models(parser=parser, users=users, account_repo=account_repo,
                                assembly_service=assembly_service, keys=keys, alert_sink=alert_sink)
    return models.extractors, models.triage
