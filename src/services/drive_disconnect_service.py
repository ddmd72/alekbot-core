"""DriveDisconnectService — ending one drive connection (MULTI_DRIVE_RFC §4.3, §4.4, §11.10; slice 2b).

The order matters and every step after the transition is repeatable, so a re-run on a DISCONNECTED connection finishes
what an interrupted run left: the connection ends (DISCONNECTED, the primary handed to the heir), its notes are archived, the credential is removed under the revoke rule when no live connection still uses it, the
adapter's caches are dropped, and a `drive_sync` toggle task lets the indexer purge the connection's index with keep
and retain its transcripts (I8, built in the indexer). Other connections of the user are never touched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional

from src.domain.drive_index_params import TICK_DEADLINE_S
from src.domain.drive_note import NOTES_PER_USER_MAX, on_connection
from src.domain.drive_task_keys import DRIVE_SYNC_TASK, scope_payload, toggle_task_key
from src.ports.drive_note_repository import DriveNoteRepository
from src.ports.task_queue import TaskQueue
from src.ports.user_drive_port import UserDrivePort
from src.utils.logger import logger

if TYPE_CHECKING:
    from src.services.credential_revocation_service import CredentialRevocationService
    from src.services.drive_connection_service import DriveConnectionService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DriveDisconnectService:
    def __init__(self, connections: "DriveConnectionService", revocation: "CredentialRevocationService",
                 drive: UserDrivePort, notes: Optional[DriveNoteRepository], nudge_queue: Optional[TaskQueue], *,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self._connections = connections
        self._revocation = revocation
        self._drive = drive
        self._notes = notes
        self._queue = nudge_queue
        self._clock = clock or _utcnow

    async def disconnect(self, user_id: str, connection_id: str, *, heir_id: Optional[str] = None) -> bool:
        """End the connection. True only on the transition to DISCONNECTED; False when it is already gone (the
        repeatable steps still run) or does not exist. DriveSettingRefused (bad heir) is raised before anything is
        written, the note archive included."""
        connection = await self._connections.get(user_id, connection_id)
        if connection is None:
            return False
        # The core refuses a bad heir before it writes anything, so a refused disconnect archives nothing; every
        # later step is repeatable, so an interruption between the two is finished by a re-run.
        ended = await self._connections.disconnect(user_id, connection_id, heir_id=heir_id)
        await self._archive_notes(user_id, connection.ref_tag)
        await self._remove_grant(user_id, connection.credential_id)
        await self._forget(connection)
        await self._nudge(connection)
        return ended

    async def _archive_notes(self, user_id: str, tag: str) -> None:
        if self._notes is None:
            return
        now = self._clock()
        archived = failed = 0
        try:
            active = (await self._notes.active(user_id, NOTES_PER_USER_MAX)
                      + await self._notes.cleared(user_id, NOTES_PER_USER_MAX))
        except Exception as exc:
            logger.error(f"Disconnect: reading the notes of user={user_id[:8]} failed: {type(exc).__name__}",
                         exc_info=True)
            return
        for note in active:
            if not on_connection(note, tag):
                continue
            seen = note.snapshot.snapshot_at if note.snapshot is not None else None
            try:
                if await self._notes.archive(user_id, note.note_id, archived_at=now, last_seen_at=seen):
                    archived += 1
            except Exception as exc:
                failed += 1
                logger.error(f"Disconnect: archiving note {note.note_id[:8]} failed: {type(exc).__name__}",
                             exc_info=True)
        logger.info(f"🗒️ Disconnect of tag {tag}: {archived} notes archived, {failed} failed")

    async def _remove_grant(self, user_id: str, credential_id: str) -> None:
        still_used = any(c.credential_id == credential_id for c in await self._connections.list_for_user(user_id))
        if not still_used:
            await self._revocation.remove(user_id, credential_id)

    async def _forget(self, connection) -> None:
        try:
            await self._drive.forget(connection.handle)
        except Exception as exc:
            logger.warning(f"Dropping the disconnected drive's caches failed: {type(exc).__name__}")

    async def _nudge(self, connection) -> None:
        if self._queue is None:
            logger.warning("Disconnect with no drive queue wired; the purge waits for the scheduler round")
            return
        try:
            await self._queue.enqueue_worker_task(
                DRIVE_SYNC_TASK, scope_payload(connection.scope), deadline_seconds=TICK_DEADLINE_S,
                dedup_key=toggle_task_key(connection.scope, self._clock()))
        except Exception as exc:
            logger.error(f"Disconnect done but the sync task was not queued: {exc}", exc_info=True)
