"""
Owner notes on drive files: wiring shared by the main service (`ServiceContainer`) and the indexer (`indexer_main.py`)
(docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.6, §4.8, §6.4, §7).

`DRIVE_NOTES_ENABLED` is the one optional kill switch (on unless set to 0/off/false/no), read with `os.getenv` by the
caller of these helpers, never registered in `load_settings()` (CLAUDE.md, `config.get()` vs `os.getenv()`). Off means
no writes anywhere: in the indexer no lifecycle (no archive checks, no audit); in the main service no tool and no
`$drive-notes` command (part A's wiring and `ConversationHandler` get no service). Reads keep working: the deleted-files
search source is added whatever the switch.

Public interface:
    drive_notes_enabled(value) -> bool
    build_drive_note_repository(db_client, env_config) -> DriveNoteRepository
    build_drive_note_erase_requests(db_client, env_config) -> EphemeralStore     # TTL policy on `expires_at`: [OWNER]
    build_drive_note_lifecycle(*, enabled, notes, index, drive) -> Optional[DriveNoteLifecycle]
    add_drive_note_deleted_source(search, notes, embeddings, *, embedding_space) -> DriveNoteDeletedSearch
"""
from __future__ import annotations

from typing import Any, Optional

from ..adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from ..adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from ..config.environment import EnvironmentConfig
from ..domain.drive_keys import note_keys
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.drive_note_repository import DriveNoteRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.ephemeral_store import EphemeralStore
from ..ports.user_drive_port import UserDrivePort
from ..services.drive_note_deleted_search import DriveNoteDeletedSearch
from ..services.drive_note_lifecycle import DriveNoteLifecycle
from ..services.drive_search_service import DriveSearchService
from ..utils.logger import logger

DRIVE_NOTES_ENABLED_ENV = "DRIVE_NOTES_ENABLED"


def drive_notes_enabled(value: Any) -> bool:
    """DRIVE_NOTES_ENABLED (§3.6): the notes kill switch, ON unless set to 0/off/false/no."""
    return str(value or "on").strip().lower() not in ("0", "off", "false", "no")


def build_drive_note_repository(db_client: Any, env_config: EnvironmentConfig) -> DriveNoteRepository:
    """`{prefix}drive_notes` (§3.1): durable, never purged with the index."""
    return FirestoreDriveNoteRepository(db_client, env_config)


def build_drive_note_erase_requests(db_client: Any, env_config: EnvironmentConfig) -> EphemeralStore:
    """The erase requests (§4.8): `{user_id}:{code}` in `{prefix}drive_note_erase_requests`, redeemed once with the
    atomic `get_and_delete`. An expired request already reads as missing; the collection's TTL policy on `expires_at`
    (an owner step, docs/07_deployment/README.md) only removes the documents nobody redeemed."""
    return FirestoreEphemeralStore(db_client, env_config.drive_note_erase_requests_collection)


def build_drive_note_lifecycle(*, enabled: bool, notes: DriveNoteRepository, index: DriveIndexRepository,
                               drive: UserDrivePort) -> Optional[DriveNoteLifecycle]:
    """The indexer's note lifecycle (§7): None with the switch off, so the tick collects no check and archives
    nothing."""
    if not enabled:
        logger.info("[Indexer] drive notes: lifecycle off (DRIVE_NOTES_ENABLED)")
        return None
    return DriveNoteLifecycle(notes, index, drive)


def add_drive_note_deleted_source(search: DriveSearchService, notes: DriveNoteRepository,
                                  embeddings: EmbeddingService, *, embedding_space: str) -> DriveNoteDeletedSearch:
    """The notes source of `search_files_in_drive` with `drive_deleted` (§6.4): one section with the archived
    transcripts. Added whatever the switch (a read)."""
    source = DriveNoteDeletedSearch(notes, embeddings, embedding_space=embedding_space, note_keys=note_keys)
    search.add_deleted_source(source)
    return source
