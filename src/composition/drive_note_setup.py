"""
Owner notes on drive files: wiring shared by the main service (`ServiceContainer`) and the indexer (`indexer_main.py`)
(docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.6, §4.8, §6.4, §7).

Part A owns this module: the kill switch's parser, the repository and the note service (with its erase-request store).
Part C appends the last three functions — the indexer's lifecycle, the main service's deleted-files source and the
`$drive-notes` command's service — and imports A's names; it builds no store of its own.

Public interface:
    DRIVE_NOTES_ENABLED_ENV, drive_notes_switch_on(raw) -> bool                    (part A)
    build_drive_note_repository(db_client, env_config) -> DriveNoteRepository      (part A)
    build_drive_note_lifecycle(*, enabled, notes, index, drive) -> Optional[DriveNoteLifecycle]     (part C)
    add_drive_note_deleted_source(search, notes, embeddings, *, embedding_space) -> DriveNoteDeletedSearch   (part C)
    command_drive_note_service(service, enabled) -> Optional[DriveNoteService]     (part C)
"""
from __future__ import annotations

from typing import Any, Optional

from ..adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from ..config.environment import EnvironmentConfig
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.drive_note_repository import DriveNoteRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.user_drive_port import UserDrivePort
from ..services.drive_note_deleted_search import DriveNoteDeletedSearch
from ..services.drive_note_lifecycle import DriveNoteLifecycle
from ..services.drive_note_service import DriveNoteService
from ..services.drive_search_service import DriveSearchService
from ..utils.logger import logger

# -- STUB (part C's worktree only): part A's names, copied from A's drive_note_setup (the switch) and assumed from the
# -- ownership ruling (the repository builder A exposes for C and B); the assembly keeps A's module and appends C's part.
DRIVE_NOTES_ENABLED_ENV = "DRIVE_NOTES_ENABLED"
_OFF = frozenset({"0", "off", "false", "no"})


def drive_notes_switch_on(raw: Optional[str]) -> bool:
    """The kill switch's value: on unless explicitly off (unset and empty are on)."""
    return str(raw or "").strip().lower() not in _OFF


def build_drive_note_repository(db_client: Any, env_config: EnvironmentConfig) -> DriveNoteRepository:
    """`{prefix}drive_notes` (§3.1): durable, never purged with the index."""
    return FirestoreDriveNoteRepository(db_client, env_config)


# -- part C ---------------------------------------------------------------------------------------------------------

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
    """The notes source of `search_files_in_drive` with `drive_deleted` (§6.4): one section with the archived
    transcripts. Added whatever the switch (a read, §3.6)."""
    source = DriveNoteDeletedSearch(notes, embeddings, embedding_space=embedding_space)
    search.add_deleted_source(source)
    return source


def command_drive_note_service(service: Optional[DriveNoteService], enabled: bool) -> Optional[DriveNoteService]:
    """The service the `$drive-notes` command gets (§3.6, §4.8): the container's, only while the switch is on — off,
    ConversationHandler gets None and answers that notes are unavailable (a write path stops with the switch)."""
    return service if enabled else None
