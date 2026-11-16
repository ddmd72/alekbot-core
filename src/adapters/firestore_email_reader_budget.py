"""Firestore daily page budget of the email index's paid scan reader (email index v2, slice 5).

One document per user (id = `user_id`) with `{user_id, day, pages}`: the pages booked on one UTC day. A booking is one
transaction that reads the document, counts `pages` only when `day` is today, refuses when the booking would pass the
daily cap and otherwise overwrites the document. One document per user, overwritten each day: no TTL and no index.
Any error is logged once and answers False (fail closed: the scan stays `scan_not_read`).
"""
import time
from datetime import datetime, timezone
from typing import Any, Callable

from google.cloud import firestore

from ..config.environment import EnvironmentConfig
from ..ports.reader_page_budget import ReaderPageBudget
from ..utils.logger import logger


class FirestoreEmailReaderBudget(ReaderPageBudget):

    def __init__(self, db_client: Any, env_config: EnvironmentConfig, pages_per_day: int,
                 clock: Callable[[], float] = time.time) -> None:
        self._db = db_client
        self._col = db_client.collection(env_config.email_reader_pages_collection)
        self._cap = pages_per_day
        self._clock = clock

    async def reserve(self, user_id: str, pages: int) -> bool:
        if pages <= 0 or pages > self._cap:
            return False
        cap = self._cap
        try:
            today = datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y-%m-%d")
            ref = self._col.document(user_id)

            @firestore.async_transactional
            async def _book(transaction: Any) -> bool:
                snap = await ref.get(transaction=transaction)
                stored = (snap.to_dict() or {}) if snap.exists else {}
                used = int(stored.get("pages", 0)) if stored.get("day") == today else 0
                if used + pages > cap:
                    return False
                transaction.set(ref, {"user_id": user_id, "day": today, "pages": used + pages})
                return True

            return await _book(self._db.transaction())
        except Exception as exc:
            logger.error(f"[EmailReaderBudget] booking failed, the scan is not read: {type(exc).__name__}")
            return False
