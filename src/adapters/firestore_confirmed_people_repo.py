"""
FirestoreConfirmedPeopleRepository — the owner-confirmed people list (docs/10_rfcs/FACT_PROFILE_RFC.md §4.5).

Collection: EnvironmentConfig.confirmed_people_collection   doc id = {account_id}
  doc: account_id, user {name, forms[]}, people [{name, forms[], relation, entry_id}] (owner's order), next_entry_id,
       source, confirmed_at
  `entry_id` (people only) and `next_entry_id` are assigned by `replace` (domain `assign_entry_ids`), never by a caller.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from google.cloud import firestore

from ..config.environment import EnvironmentConfig
from ..domain.confirmed_people import (
    CONFIRMED_SOURCES, ConfirmedPeople, assign_entry_ids, confirmed_people_document, parse_confirmed_people,
)
from ..ports.confirmed_people_repository import ConfirmedPeopleRepository


class FirestoreConfirmedPeopleRepository(ConfirmedPeopleRepository):
    """One document per account: read whole by one `get`, replaced whole by one `set`. No index is needed. The client
    is injected, so the `database=` choice stays with the caller."""

    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._lists = db_client.collection(env_config.confirmed_people_collection)

    async def get(self, account_id: str) -> Optional[ConfirmedPeople]:
        snap = await self._lists.document(account_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        if data.get("account_id") != account_id:
            raise ValueError("the stored confirmed people list does not belong to the account")
        return parse_confirmed_people(data)

    async def replace(self, account_id: str, people: ConfirmedPeople, source: str) -> None:
        if source not in CONFIRMED_SOURCES:
            raise ValueError(f"unknown source {source!r}; expected one of {CONFIRMED_SOURCES}")
        ref = self._lists.document(account_id)

        @firestore.async_transactional
        async def _write(transaction) -> None:
            snap = await ref.get(transaction=transaction)
            stored: Optional[ConfirmedPeople] = None
            if snap.exists:
                data = snap.to_dict() or {}
                if data.get("account_id") != account_id:
                    raise ValueError("the stored confirmed people list does not belong to the account")
                stored = parse_confirmed_people(data)
            assigned, _ = assign_entry_ids(people, stored)
            transaction.set(ref, {"account_id": account_id, **confirmed_people_document(assigned), "source": source,
                                  "confirmed_at": datetime.now(timezone.utc)})

        await _write(self._db.transaction())
