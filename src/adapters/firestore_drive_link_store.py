"""Firestore storage of the drive connector's one-time download links (DRIVE_MCP_CONNECTOR_RFC §8).

Collection `{prefix}drive_links` (`EnvironmentConfig.drive_links_collection`); doc id = the link key (the keyed digest
of the raw id: no raw id in any field or id); fields: every `DriveLink` field but the key. A TTL policy on
`purge_after` deletes the record a day after issue (docs/07_deployment/README.md); reads check expiry themselves.
`consume` is one transaction over the one document (modelled on `FirestoreEphemeralStore.get_and_delete`): of two
concurrent consumes one commits, the other re-runs and finds the read spent.
"""
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore

from ..config.environment import EnvironmentConfig
from ..domain.drive_link import DriveLink
from ..ports.drive_link_store import DriveLinkStore


def _utc(value: Any) -> datetime:
    """A plain tz-aware UTC datetime from Firestore's DatetimeWithNanoseconds."""
    return datetime(value.year, value.month, value.day, value.hour, value.minute, value.second, value.microsecond,
                    tzinfo=timezone.utc)


def _to_fs(link: DriveLink) -> Dict[str, Any]:
    data = asdict(link)
    del data["link_key"]
    return data


def _from_fs(link_key: str, data: Dict[str, Any]) -> DriveLink:
    used = data.get("used_at")
    return DriveLink(
        link_key=link_key, user_id=data["user_id"], account_id=data["account_id"],
        connection_id=data["connection_id"], ref_tag=data["ref_tag"], item_id=data["item_id"],
        grant_id=data["grant_id"], created_at=_utc(data["created_at"]), expires_at=_utc(data["expires_at"]),
        purge_after=_utc(data["purge_after"]), reads=int(data.get("reads", 0)),
        used_at=_utc(used) if used is not None else None,
    )


class FirestoreDriveLinkStore(DriveLinkStore):
    def __init__(self, db_client, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._col = db_client.collection(env_config.drive_links_collection)

    async def create(self, link: DriveLink) -> None:
        await self._col.document(link.link_key).create(_to_fs(link))

    async def peek(self, link_key: str, now: datetime, max_reads: int) -> Optional[DriveLink]:
        snap = await self._col.document(link_key).get()
        if not snap.exists:
            return None
        link = _from_fs(link_key, snap.to_dict())
        return link if link.live(now, max_reads) else None

    async def consume(self, link_key: str, now: datetime, max_reads: int) -> Optional[DriveLink]:
        ref = self._col.document(link_key)

        @firestore.async_transactional
        async def _consume(transaction) -> Optional[DriveLink]:
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return None
            link = _from_fs(link_key, snap.to_dict())
            if not link.live(now, max_reads):
                return None
            transaction.update(ref, {"reads": link.reads + 1, "used_at": link.used_at or now})
            return link

        return await _consume(self._db.transaction())
