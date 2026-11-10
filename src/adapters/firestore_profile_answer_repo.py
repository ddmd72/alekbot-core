"""
FirestoreProfileAnswerRepository — the owner's answers to profile questions (docs/10_rfcs/FACT_PROFILE_RFC.md §4.7).

Collection: EnvironmentConfig.profile_answers_collection   doc id = {account_id}
  doc: account_id, places [{key, city, country, resolution, geonameid, question_id, answered_at}],
       people [{key, lineage_id, mention, resolution, name, question_id, answered_at}],
       dates [{key, lineage_id, mention, resolution, start, end, precision, question_id, answered_at}], updated_at
"""
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from ..config.environment import EnvironmentConfig
from ..domain.profile_answers import (
    EMPTY_ANSWERS, OwnerAnswer, ProfileAnswers, parse_profile_answers, profile_answers_document,
)
from ..ports.profile_answer_repository import ProfileAnswerRepository


def _parsed(account_id: str, data: Any) -> ProfileAnswers:
    data = data or {}
    if data.get("account_id") != account_id:
        raise ValueError("the stored profile answers do not belong to the account")
    return parse_profile_answers(data)


class FirestoreProfileAnswerRepository(ProfileAnswerRepository):
    """One document per account: read whole by one `get`, extended by one transaction per answer. No index is needed.
    The client is injected, so the `database=` choice stays with the caller."""

    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._answers = db_client.collection(env_config.profile_answers_collection)

    async def get(self, account_id: str) -> ProfileAnswers:
        snap = await self._answers.document(account_id).get()
        if not snap.exists:
            return EMPTY_ANSWERS
        return _parsed(account_id, snap.to_dict())

    async def record(self, account_id: str, answer: OwnerAnswer) -> bool:
        ref = self._answers.document(account_id)

        @firestore.async_transactional
        async def _record(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            current = _parsed(account_id, snap.to_dict()) if snap.exists else EMPTY_ANSWERS
            if current.has(answer.key):
                return False
            document = {"account_id": account_id, **profile_answers_document(current.with_answer(answer)),
                        "updated_at": datetime.now(timezone.utc)}
            transaction.set(ref, document)
            return True

        recorded: bool = await _record(self._db.transaction())
        return recorded
