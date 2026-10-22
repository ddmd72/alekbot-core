"""
Drive-note wiring shared by the main service (`ServiceContainer`) and, from S2, the indexer service
(docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.6, §4.4).

`DRIVE_NOTES_ENABLED` is the optional kill switch, read with `os.getenv` by the composition root (never registered in
`load_settings()`, CLAUDE.md "config.get() vs os.getenv()"). Default on; `0`, `off`, `false`, `no` turn it off. Off
means: no tool declared, no `$drive-notes` command and, in the indexer, no note lookup, notes step or archiving. The
store and the service are still built, so reads (the deleted-files search, the open header) keep answering.
"""
from __future__ import annotations

from typing import Any, Optional

from ..adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from ..adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from ..adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from ..adapters.security.composite_adapter import CompositeAdapter
from ..adapters.security.regex_adapter import RegexSecurityAdapter
from ..config.environment import EnvironmentConfig
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..services.drive_note_service import DriveNoteService

DRIVE_NOTES_ENABLED_ENV = "DRIVE_NOTES_ENABLED"
_OFF = frozenset({"0", "off", "false", "no"})


def drive_notes_switch_on(raw: Optional[str]) -> bool:
    """The kill switch's value: on unless explicitly off (unset and empty are on)."""
    return str(raw or "").strip().lower() not in _OFF


def build_drive_note_service(db_client: Any, env_config: EnvironmentConfig, *, drive: UserDrivePort,
                             index: DriveIndexRepository, embeddings: EmbeddingService, users: UserRepository,
                             nudge_queue: Optional[TaskQueue]) -> DriveNoteService:
    """The note service over the Firestore store (`{prefix}drive_notes`) and the erase requests'
    ephemeral store (`{prefix}drive_note_erase_requests`, TTL policy on `expires_at`: an owner step). The provider key
    stays the domain's ONEDRIVE_PROVIDER_KEY (the service default), the one the indexer uses too. `users` must be
    uncached: it gives the owner's indexing switch for the receipt's status."""
    return DriveNoteService(
        FirestoreDriveNoteRepository(db_client, env_config), drive, index, embeddings,
        CompositeAdapter(adapters=[RegexSecurityAdapter()], strategy="worst_case"), users,
        embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE,
        nudge_queue=nudge_queue,
        erase_requests=FirestoreEphemeralStore(db_client, env_config.drive_note_erase_requests_collection),
    )


__all__ = ["DRIVE_NOTES_ENABLED_ENV", "build_drive_note_service", "drive_notes_switch_on"]
