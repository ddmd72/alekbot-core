"""
The `--user` / `--connection` arguments of the owner's drive scripts (MULTI_DRIVE_RFC §8, §12 "After acceptance"): from
slice 1b a drive script acts on one connection. `--connection` names it; without it the user's one live connection
(ACTIVE or NEEDS_RECONNECT) is taken, and a user with none or several is refused with the list of ids, so a script
never guesses which drive it writes to.

Not a script: imported by drive_index_rebuild.py, drive_transcribe_flag.py, drive_initial_build_reset.py,
drive_transcribe_requeue.py, drive_mirror_status.py and drive_transcript_sweep.py.
"""
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.drive_connection import DriveConnection  # noqa: E402
from src.domain.drive_connection_state import ConnectionState  # noqa: E402
from src.ports.drive_connection_repository import DriveConnectionRepository  # noqa: E402

LIVE = (ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT)


async def resolve_connection(repo: DriveConnectionRepository, user_id: str,
                             connection_id: Optional[str]) -> Tuple[Optional[DriveConnection], str]:
    """(the connection, "") or (None, why it was refused)."""
    if connection_id:
        conn = await repo.get(user_id, connection_id)
        if conn is None:
            return None, f"no connection {connection_id} for this user"
        if conn.state not in LIVE:
            return None, f"connection {connection_id} is {conn.state.value}"
        return conn, ""
    live: List[DriveConnection] = [c for c in await repo.list_for_user(user_id) if c.state in LIVE]
    if len(live) == 1:
        return live[0], ""
    if not live:
        return None, "the user has no live drive connection"
    ids = ", ".join(f"{c.connection_id} ({c.mount_name})" for c in live)
    return None, f"the user has {len(live)} live drive connections; name one with --connection: {ids}"


async def account_of(db: Any, credentials_collection: str, user_id: str, credential_id: str) -> Tuple[str, str]:
    """(realm, subject) of a stored credential, read from its plaintext fields (never a decrypt); ("", "") when absent."""
    from google.cloud.firestore import FieldFilter

    query = (db.collection(credentials_collection).where(filter=FieldFilter("user_id", "==", user_id))
             .where(filter=FieldFilter("credential_id", "==", credential_id)))
    for snap in await query.get():
        account = (snap.to_dict() or {}).get("account") or {}
        return str(account.get("realm") or ""), str(account.get("subject") or "")
    return "", ""
