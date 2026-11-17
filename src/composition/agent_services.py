"""
The container's shared services that `UserAgentFactory` takes, as one typed bundle.

`ServiceContainer.agent_services()` builds it and `build_application_graph` passes it as `services=`. It replaces a
dict unpacked with `**` into the factory's keywords: a stringly typed seam the type checker cannot see, the same
reason `decisions/application_graph_builder.md` rejected `blueprint_kwargs`. A field is `Optional` exactly where the
container can hold `None`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..adapters.fact_management_adapter import FactManagementAdapter
from ..ports.agent_note_port import AgentNotePort
from ..ports.confirmed_people_repository import ConfirmedPeopleRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..ports.fact_write_port import FactWritePort
from ..ports.file_storage_port import FileStoragePort
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..ports.llm_port import LLMPort
from ..ports.prompt_content_store import PromptContentStore
from ..ports.recurrence_port import RecurrencePort
from ..ports.repository import FactRepository
from ..ports.session_store import SessionStore
from ..ports.tasks_provider_port import TasksProviderPort
from ..services.agent_context_builder import AgentContextBuilder
from ..services.biographical_context_service import BiographicalContextService
from ..services.configuration_service import ConfigurationService
from ..services.drive_note_service import DriveNoteService
from ..services.drive_search_service import DriveSearchService
from ..services.email_card_search_service import EmailCardSearchService
from ..services.email_search_service import EmailSearchService
from ..services.file_conversion_service import FileConversionService
from ..services.memory_fact_service import MemoryFactService
from ..services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from ..services.provider_registry import ProviderRegistry
from ..services.search_enrichment_service import SearchEnrichmentService
from ..services.skill_service import SkillService
from ..services.task_indexing_service import TaskIndexingService
from ..services.user_drive_service import UserDriveService
from ..services.workspace_service import WorkspaceService


@dataclass(frozen=True)
class AgentServices:
    """The shared services every user's agents are built from; one per process, built by the container."""

    session_store: SessionStore
    llm_port: LLMPort
    claude_service: LLMPort
    grok_service: Optional[LLMPort]
    openai_service: Optional[LLMPort]
    embedding_service: EmbeddingService
    repository: FactRepository
    config_service: ConfigurationService
    biographical_context_service: BiographicalContextService
    registry: ProviderRegistry
    context_builder: AgentContextBuilder
    assembly_service: Optional[PromptAssemblyService]
    fact_write_service: FactWritePort
    fact_management_adapter_factory: Callable[[SearchEnrichmentService], FactManagementAdapter]
    email_search_service: EmailSearchService
    email_card_search: EmailCardSearchService
    indexed_email_repo: IndexedEmailRepository
    confirmed_people: ConfirmedPeopleRepository
    tasks_provider: Optional[TasksProviderPort]
    task_indexing: Optional[TaskIndexingService]
    notes_provider: AgentNotePort
    recurrence: RecurrencePort
    file_conversion_service: Optional[FileConversionService]
    file_storage: Optional[FileStoragePort]
    user_drive_service: Optional[UserDriveService]
    drive_search_service: Optional[DriveSearchService]
    prompt_content_store: Optional[PromptContentStore]
    skill_service: SkillService
    memory_fact_service: MemoryFactService
    lifecycle_counters: FactLifecycleCountersPort
    workspace_service: Optional[WorkspaceService]
    drive_note_service: Optional[DriveNoteService]
    drive_notes_enabled: bool
