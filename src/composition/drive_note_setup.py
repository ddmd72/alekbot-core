"""
Drive-note wiring shared by the main service (`ServiceContainer`) and the indexer service (`indexer_main.py`)
(docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.6, §4.4, §4.8, §6.4, §7).

`DRIVE_NOTES_ENABLED` is the optional kill switch, read with `os.getenv` by the composition root (never registered in
`load_settings()`, CLAUDE.md "config.get() vs os.getenv()"). Default on; `0`, `off`, `false`, `no` turn it off. Off
means: no tool declared, no `$drive-notes` command and, in the indexer, no note lookup, notes step or archiving. The
store and the service are still built, so reads (the deleted-files search, the open header) keep answering.

One note store per process: the container (or the indexer) builds the repository once with
`build_drive_note_repository` and hands that one instance to every consumer.

Public interface:
    DRIVE_NOTES_ENABLED_ENV, drive_notes_switch_on(raw) -> bool
    build_drive_note_repository(db_client, env_config) -> DriveNoteRepository
    build_drive_note_service(db_client, env_config, *, notes, drive, index, embeddings, users, security_port, nudge_queue)
        -> DriveNoteService
    build_drive_note_lifecycle(*, enabled, notes, index, drive) -> Optional[DriveNoteLifecycle]
    add_drive_note_deleted_source(search, notes, embeddings, *, embedding_space) -> DriveNoteDeletedSearch
    command_drive_note_service(service, enabled) -> Optional[DriveNoteService]
"""
from __future__ import annotations

from typing import Any, Optional

from ..adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from ..adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from ..adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from ..config.environment import EnvironmentConfig
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.drive_note_repository import DriveNoteRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.security_port import SecurityPort
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..services.drive_note_deleted_search import DriveNoteDeletedSearch
from ..services.drive_note_lifecycle import DriveNoteLifecycle
from ..services.drive_note_service import DriveNoteService
from ..services.drive_search_service import DriveSearchService
from ..utils.logger import logger

DRIVE_NOTES_ENABLED_ENV = "DRIVE_NOTES_ENABLED"
_OFF = frozenset({"0", "off", "false", "no"})


def drive_notes_switch_on(raw: Optional[str]) -> bool:
    """The kill switch's value: on unless explicitly off (unset and empty are on)."""
    return str(raw or "").strip().lower() not in _OFF


def build_drive_note_repository(db_client: Any, env_config: EnvironmentConfig) -> DriveNoteRepository:
    """`{prefix}drive_notes` (§3.1): durable, never purged with the index. Built once per process."""
    return FirestoreDriveNoteRepository(db_client, env_config)


def build_drive_note_service(db_client: Any, env_config: EnvironmentConfig, *, notes: DriveNoteRepository,
                             drive: UserDrivePort, index: DriveIndexRepository, embeddings: EmbeddingService,
                             users: UserRepository, security_port: SecurityPort, nudge_queue: Optional[TaskQueue]) -> DriveNoteService:
    """The note service over the process's note store (`notes`, from `build_drive_note_repository`) and the erase
    requests' ephemeral store (`{prefix}drive_note_erase_requests`, TTL policy on `expires_at`: an owner step). The
    provider key stays the domain's ONEDRIVE_PROVIDER_KEY (the service default), the one the indexer uses too. `users`
    must be uncached: it gives the owner's indexing switch for the receipt's status. `security_port` is the
    container's one stateless port."""
    return DriveNoteService(
        notes, drive, index, embeddings,
        security_port, users,
        embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE,
        nudge_queue=nudge_queue,
        erase_requests=FirestoreEphemeralStore(db_client, env_config.drive_note_erase_requests_collection),
    )


def build_drive_note_lifecycle(*, enabled: bool, notes: Optional[DriveNoteRepository], index: DriveIndexRepository,
                               drive: UserDrivePort) -> Optional[DriveNoteLifecycle]:
    """The indexer's note lifecycle (§7): None with the switch off (or no store), so the tick collects no check and
    archives nothing."""
    if not enabled or notes is None:
        logger.info("[Indexer] drive notes: lifecycle off (DRIVE_NOTES_ENABLED)")
        return None
    return DriveNoteLifecycle(notes, index, drive)


def add_drive_note_deleted_source(search: DriveSearchService, notes: DriveNoteRepository,
                                  embeddings: EmbeddingService, *, embedding_space: str) -> DriveNoteDeletedSearch:
    """The notes source of the drive specialist's deleted-files search (`search_deleted_recordings_and_notes`, §6.4): one
    section with the archived transcripts. Added whatever the switch (a read, §3.6)."""
    source = DriveNoteDeletedSearch(notes, embeddings, embedding_space=embedding_space)
    search.add_deleted_source(source)
    return source


def command_drive_note_service(service: Optional[DriveNoteService], enabled: bool) -> Optional[DriveNoteService]:
    """The service the `$drive-notes` command gets (§3.6, §4.8): the container's, only while the switch is on — off,
    ConversationHandler gets None and answers that notes are unavailable (a write path stops with the switch)."""
    return service if enabled else None


__all__ = [
    "DRIVE_NOTES_ENABLED_ENV", "add_drive_note_deleted_source", "build_drive_note_lifecycle",
    "build_drive_note_repository", "build_drive_note_service", "command_drive_note_service", "drive_notes_switch_on",
]
