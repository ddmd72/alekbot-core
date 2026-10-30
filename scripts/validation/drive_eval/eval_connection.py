"""The drive evals' one connection (MULTI_DRIVE_RFC §4.4, §6, §9): the fixture drive mounted as a single ACTIVE connection
of the eval user, so the services (search, operations, notes) resolve refs and the primary exactly as production does.

`EvalDriveConnections` is a DriveConnectionService over that one connection: the read methods the services call
(`get`, `list_for_user`, `handle_for`, `primary`, `primary_handle`, `status`, `resolve_ref`) answer from it, with the
production rules (a tag of no connection and an untagged ref answer the stale line); every writer stays the base
class's. `ref_item_id` reads the item id out of a tagged ref, or an untagged one of before the cut-over (the query
vocabulary reads stored history).

Public interface:
    EVAL_CONNECTION_ID, EVAL_REF_TAG, EVAL_KIND
    eval_connection(user_id, *, indexing_enabled=True, transcribe_enabled=False) -> DriveConnection
    EvalDriveConnections(connection)            a DriveConnectionService answering for that one connection
    ref_item_id(text) -> Optional[str]          the item id of the first `drive:` ref in `text`, tagged or not
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Union

from src.domain import drive_ref
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_connection_status import DriveConnectionStatus
from src.domain.drive_handle import DriveHandle
from src.domain.drive_ref import DriveRef
from src.domain.drive_ref_refusal import DriveRefRefusal, disconnected_ref_line, stale_ref_line
from src.domain.drive_resolved_ref import ResolvedDriveRef
from src.domain.user_drive import DriveNotConnectedError
from src.services.drive_connection_service import DriveConnectionService

EVAL_CONNECTION_ID = "conn-eval"
EVAL_REF_TAG = "ev"
EVAL_KIND = "fixture_drive"
_EVAL_CREDENTIAL = "cred-eval"
_EVAL_DRIVE_IDENTITY = "fixture_drive:eval"
_CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def eval_connection(user_id: str, *, indexing_enabled: bool = True, transcribe_enabled: bool = False,
                    account_id: str = "eval-account") -> DriveConnection:
    """The eval user's one connection: ACTIVE, primary, tag `ev`."""
    return DriveConnection(connection_id=EVAL_CONNECTION_ID, user_id=user_id, account_id=account_id,
                           credential_id=_EVAL_CREDENTIAL, kind=EVAL_KIND, drive_identity=_EVAL_DRIVE_IDENTITY,
                           ref_tag=EVAL_REF_TAG, indexing_enabled=indexing_enabled,
                           transcribe_enabled=transcribe_enabled, created_at=_CREATED)


class EvalDriveConnections(DriveConnectionService):
    """The read side of DriveConnectionService over one fixed connection (no repository, no credential store)."""

    def __init__(self, connection: DriveConnection) -> None:
        super().__init__(connections=None, credentials=None)  # type: ignore[arg-type]   # never read: overridden
        self._one = connection

    def _mine(self, user_id: str) -> Optional[DriveConnection]:
        return self._one if user_id == self._one.user_id else None

    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        one = self._mine(user_id)
        return one if one is not None and one.connection_id == connection_id else None

    async def list_for_user(self, user_id: str, *, include_disconnected: bool = False) -> List[DriveConnection]:
        one = self._mine(user_id)
        if one is None or (one.state is ConnectionState.DISCONNECTED and not include_disconnected):
            return []
        return [one]

    async def handle_for(self, user_id: str, connection_id: str) -> DriveHandle:
        one = await self.get(user_id, connection_id)
        if one is None or one.state is ConnectionState.DISCONNECTED:
            raise DriveNotConnectedError("no such live connection")
        return one.handle

    async def primary(self, user_id: str) -> Optional[DriveConnection]:
        one = self._mine(user_id)
        return one if one is not None and one.state is ConnectionState.ACTIVE else None

    async def primary_handle(self, user_id: str) -> DriveHandle:
        one = await self.primary(user_id)
        if one is None:
            raise DriveNotConnectedError("no primary drive connection")
        return one.handle

    async def status(self, user_id: str) -> DriveConnectionStatus:
        return DriveConnectionStatus.of(self._mine(user_id))

    async def resolve_ref(self, user_id: str, text: str) -> Optional[Union[ResolvedDriveRef, DriveRefRefusal]]:
        parsed = drive_ref.parse(text)
        if parsed is None:
            return None
        one = self._mine(user_id)
        if not isinstance(parsed, DriveRef) or one is None or parsed.tag != one.ref_tag:
            return DriveRefRefusal(stale_ref_line(str(parsed)))
        if one.state is ConnectionState.DISCONNECTED:
            return DriveRefRefusal(disconnected_ref_line(str(parsed)))
        return ResolvedDriveRef(connection=one, item_id=parsed.item_id)


def ref_item_id(text: str) -> Optional[str]:
    """The item id of the first `drive:` ref in `text`: a tagged ref's, or the body of an untagged one (stored history of
    before the cut-over); None when there is no drive ref."""
    parsed = drive_ref.parse(text or "")
    if parsed is None:
        return None
    if isinstance(parsed, DriveRef):
        return parsed.item_id
    return parsed.raw[len(drive_ref.REF_PREFIX):] or None
