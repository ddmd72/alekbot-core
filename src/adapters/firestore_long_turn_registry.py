"""Firestore LongTurnRegistry. `expires_at` feeds the collection's TTL policy."""
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from google.cloud.firestore import FieldFilter

from ..domain.long_turn import LongTurnRecord, LongTurnStatus
from ..ports.long_turn_registry import LongTurnRegistry
from ..utils.logger import logger

_TTL = timedelta(days=2)


class FirestoreLongTurnRegistry(LongTurnRegistry):
    def __init__(self, db_client, collection: str) -> None:
        self._db = db_client
        self._collection = collection

    def _doc(self, turn_id: str):
        return self._db.collection(self._collection).document(turn_id)

    async def start(self, record: LongTurnRecord) -> None:
        data = record.model_dump(mode="json")
        data["expires_at"] = datetime.now(timezone.utc) + _TTL
        await self._doc(record.turn_id).set(data)

    async def get(self, turn_id: str) -> Optional[LongTurnRecord]:
        snap = await self._doc(turn_id).get()
        if not snap.exists:
            return None
        data = {k: v for k, v in snap.to_dict().items() if k != "expires_at"}
        return LongTurnRecord(**data)

    async def heartbeat(self, turn_id: str, step: str) -> bool:
        doc = self._doc(turn_id)
        await doc.update({"heartbeat_at": time.time(), "step": step})
        snap = await doc.get()
        return bool(snap.exists and snap.to_dict().get("cancel_requested"))

    async def finish(self, turn_id: str, status: LongTurnStatus) -> None:
        await self._doc(turn_id).update({"status": status.value})

    async def list_running(self, user_id: str) -> List[LongTurnRecord]:
        query = (
            self._db.collection(self._collection)
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("status", "==", LongTurnStatus.RUNNING.value))
        )
        out: List[LongTurnRecord] = []
        async for snap in query.stream():
            data = {k: v for k, v in snap.to_dict().items() if k != "expires_at"}
            out.append(LongTurnRecord(**data))
        return out

    async def request_cancel(self, user_id: str, turn_id: str) -> bool:
        record = await self.get(turn_id)
        if record is None or record.user_id != user_id or record.status is not LongTurnStatus.RUNNING:
            logger.info("[LongTurnRegistry] cancel refused for %s", turn_id)
            return False
        await self._doc(turn_id).update({"cancel_requested": True})
        return True
