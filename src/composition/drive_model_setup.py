"""
The drive indexer's model wiring (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.2, §24.9; D7, D12).

Builds, for `indexer_main.py` only: a ProviderRegistry with the two providers that take inline file bytes (Claude,
OpenAI; Gemini's adapter does not, §24.2), an AgentContextBuilder over it with a process-local breaker, the
Firestore prompt builder, the quota service that bills each call to the user's account, and the two runners. The
extractor registry is `{PARSER: parser, READER: reader runner}` (EXTRACTOR_CHAINS ids); the reader cuts PDFs with
the same sandboxed parser.

Deliberately absent: prompt caching (the prompts are below the providers' minimum cacheable size), the alerting
proxy (a scan the provider refuses is a per-file outcome, not a Slack alert), the prompt-content store (verbatim
scans stay out of BigQuery, §24.2), and Logfire. A provider without a key is not registered: a user whose
configuration names it gets a model outage (logged ERROR), parsing goes on.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from ..adapters.claude_adapter import ClaudeAdapter
from ..adapters.firestore_quota_service import FirestoreQuotaService
from ..adapters.in_memory_provider_resilience import InMemoryProviderResilience
from ..adapters.openai_adapter import OpenAIAdapter
from ..domain.drive_extraction import PARSER, READER
from ..ports.account_repository import AccountRepository
from ..ports.alert_sink import AlertSinkPort
from ..ports.content_extractor_port import ContentExtractorPort
from ..ports.image_triage_port import ImageTriagePort
from ..ports.user_repository import UserRepository
from ..services.agent_context_builder import AgentContextBuilder
from ..services.prompt_builder import PromptBuilder
from ..services.provider_registry import ProviderRegistry
from ..utils.logger import logger
from .drive_reader_runner import DriveReaderRunner, PdfSplitter
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
    """Claude and OpenAI, each only when its key is set. Keys are stripped: a secret stored with a trailing
    newline becomes an illegal header that looks like a network error."""
    registry: ProviderRegistry = ProviderRegistry()
    anthropic_key = (keys.get("ANTHROPIC_API_KEY") or "").strip()
    openai_key = (keys.get("OPENAI_API_KEY") or "").strip()
    if anthropic_key:
        registry.register("claude", ClaudeAdapter(api_key=anthropic_key))
    if openai_key:
        registry.register("openai", OpenAIAdapter(api_key=openai_key))
    missing = [name for name, key in (("ANTHROPIC_API_KEY", anthropic_key), ("OPENAI_API_KEY", openai_key))
               if not key]
    if missing:
        logger.warning(f"[Indexer] {', '.join(missing)} not set: the drive models on that provider are unavailable")
    return registry


def build_drive_model_extractors(*, parser: Any, users: UserRepository, account_repo: AccountRepository,
                                 assembly_service: Any, keys: Mapping[str, Optional[str]],
                                 alert_sink: Optional[AlertSinkPort] = None,
                                 ) -> Tuple[Dict[str, ContentExtractorPort], ImageTriagePort]:
    """The extractor registry and the triage port for DriveIndexingService. `parser` is the child-process parser
    (a ContentExtractorPort with `pdf_page_count` and `cut_pdf_window`)."""
    registry = build_drive_model_registry(keys)
    context_builder = AgentContextBuilder(registry, resilience_port=InMemoryProviderResilience())
    prompt_builder = _DrivePromptBuilder(repo=None, assembly_service=assembly_service)
    quota = FirestoreQuotaService(account_repo, alert_sink=alert_sink)
    splitter: PdfSplitter = parser
    reader = DriveReaderRunner(context_builder, users, prompt_builder, splitter, quota_service=quota)
    triage = DriveTriageRunner(context_builder, users, prompt_builder, quota_service=quota)
    return {PARSER: parser, READER: reader}, triage

