"""
The drive side of the drive evals after the move to the specialist (DRIVE_SPECIALIST_RFC §12): one coordinator over
the fixture drive and its index, holding the drive specialist and FileManagement, both on ONE DriveOperations — the
production wiring (§5.5), with the fixture's services. G3 (`smart_dry_run`) and the specialist eval (`specialist_ab`,
its replay) route Smart's `work_with_drive_files` and its `open_file` on a `drive:` ref here.

The specialist is built by `build_drive_specialist`, the factory's own builder, with the user's real config, prompt
builder and context builder: the eval measures the production specialist, prompt included. No notification service
is wired: a receipt, a late-failure notice or a video sent to chat cannot reach the owner; no ops alert sink either
(a turn-cap exit is logged, never posted). Writes are stubbed by `dry_run_drive.DryRunDriveOperations`, which the
caller installs before building anything.

    fixture = build_drive_fixture(user_id, drive, search, user_config=..., prompt_builder=..., context_builder=...)
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from src.agents.drive_agent import DriveAgent
from src.agents.file_management_agent import FileManagementAgent
from src.composition.drive_specialist_build import build_drive_specialist
from src.domain.agent import AgentConfig
from src.domain.drive_connection import DriveConnection
from src.domain.drive_search import DriveSearchRequest, DriveSearchResult
from src.domain.drive_search_line import SearchLine
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, FILE_MANAGEMENT
from src.infrastructure.agent_registry import AgentRegistry
from src.infrastructure.delegation_engine import DelegationEngine
from src.infrastructure.drive_operations import DriveOperations
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

from .eval_connection import EvalDriveConnections, eval_connection
from .runner import EVAL_USER


class EvalUserSearch:
    """The index the eval built under EVAL_USER, asked as that user whoever asks (the dry run's Smart is the owner).
    With `off` set, `search` answers as for a user whose drive search is off (a drive-off case, final review F5-6),
    and so do `search_state`, the specialist's state-block line, and `excerpts` / `excerpt_range`, the passages reads:
    production reads one flag for all of them. Every other
    DriveSearchService method (search_deleted, access_note, transcript_text, archived_text, …) is forwarded with
    EVAL_USER in place of the caller's id: an unforwarded method once failed every deleted-files search in the spike
    (spike report, index cache note)."""

    def __init__(self, search: Any) -> None:
        self._search = search
        self.off = False

    async def search(self, user_id: str, request: DriveSearchRequest) -> DriveSearchResult:
        if self.off:
            from src.domain.drive_search import STATUS_DISABLED, DriveSearchEnvelope
            return DriveSearchResult(status=STATUS_DISABLED, envelope=DriveSearchEnvelope(enabled=False),
                                     request=request)
        result: DriveSearchResult = await self._search.search(EVAL_USER, request)
        return result

    async def search_state(self, user_id: str) -> SearchLine:
        if self.off:
            return SearchLine.OFF
        state: SearchLine = await self._search.search_state(EVAL_USER)
        return state

    async def excerpts(self, user_id: str, request: Any, **kwargs: Any) -> Any:
        """find_passages' read: gated like `search` (production gates it with the same flag, `_excerpt_gate`), so a
        drive-off case gets the `disabled` answer, not the real index (answer from content, plan S2.6 follow-up)."""
        if self.off:
            return self._disabled(request)
        return await self._search.excerpts(EVAL_USER, request, **kwargs)

    async def excerpt_range(self, user_id: str, request: Any) -> Any:
        """read_passages' read: the same gate."""
        if self.off:
            return self._disabled(request)
        return await self._search.excerpt_range(EVAL_USER, request)

    @staticmethod
    def _disabled(request: Any) -> Any:
        from src.domain.drive_excerpt_result import ExcerptResult
        from src.domain.drive_search import STATUS_DISABLED
        return ExcerptResult(status=STATUS_DISABLED, request=request)

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._search, name)
        if not callable(target):
            return target

        async def as_eval_user(first: Any, *args: Any, **kwargs: Any) -> Any:
            # a connection-first read (access_note, text_extent, transcript_text, archived_text) gets the eval user's
            # copy of the connection; a user-first one the eval user
            if isinstance(first, DriveConnection):
                return await target(replace(first, user_id=EVAL_USER), *args, **kwargs)
            return await target(EVAL_USER, *args, **kwargs)
        return as_eval_user


class _NoChatFiles:
    """The dry run has no chat uploads: every chat-file read is a missing file (smart_dry_run's FileStoragePort)."""

    async def upload(self, data: bytes, filename: str, user_id: str, content_type: str) -> str:
        raise FileNotFoundError(filename)

    async def download(self, filename: str, user_id: str) -> bytes:
        raise FileNotFoundError(filename)

    async def delete(self, filename: str, user_id: str) -> None:
        raise FileNotFoundError(filename)

    async def exists(self, filename: str, user_id: str) -> bool:
        return False

    async def get_url(self, filename: str, user_id: str) -> str:
        raise FileNotFoundError(filename)


@dataclass
class DriveFixture:
    engine: DelegationEngine          # dispatches into the fixture coordinator
    coordinator: AgentCoordinator
    operations: DriveOperations       # the one instance both agents hold
    file_management: FileManagementAgent
    drive_agent: Optional[DriveAgent]
    search: EvalUserSearch


def build_drive_fixture(user_id: str, drive: Any, search: Any, *, user_config: Any = None, prompt_builder: Any = None,
                        context_builder: Any = None) -> DriveFixture:
    """FileManagement always; the specialist when a user config, prompt builder and context builder are given (a
    harness without a model — the offline tests of the metrics — needs only the reads)."""
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    coordinator = AgentCoordinator(registry=registry)
    storage = _NoChatFiles()
    as_user = EvalUserSearch(search)
    connections = EvalDriveConnections(eval_connection(user_id))   # the case user's one connection (tag `ev`)
    conversion = FileConversionService(storage=storage, drive=drive, connections=connections)
    service = UserDriveService(drive, connections)
    operations = DriveOperations(conversion_service=conversion, drive_service=service, drive_search=as_user)
    files = FileManagementAgent(
        config=AgentConfig(agent_id=f"{FILE_MANAGEMENT.agent_id}_{user_id}", agent_type="file_management",
                           timeout_ms=120_000, capabilities=["file_storage"]),
        conversion_service=conversion, storage=storage, drive_service=service, drive_search=as_user,
        drive_operations=operations)
    coordinator.register_agent(files)
    agent: Optional[DriveAgent] = None
    if user_config is not None and prompt_builder is not None and context_builder is not None:
        agent = build_drive_specialist(user_id, context_builder=context_builder, prompt_builder=prompt_builder,
                                       user_config=user_config, operations=operations, alert_sink=None)
        agent.coordinator = coordinator
        coordinator.register_agent(agent)
    return DriveFixture(DelegationEngine(coordinator), coordinator, operations, files, agent, as_user)
