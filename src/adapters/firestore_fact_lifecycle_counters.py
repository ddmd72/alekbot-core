"""
FirestoreFactLifecycleCounters — FactLifecycleCountersPort on Firestore (FACT_LIFECYCLE_RFC D10).

`{prefix}fact_lifecycle_daily`, doc id `<account_id>:<YYYY-MM-DD>` (UTC day): `account_id`, `day` (ISO string), the
janitor's snapshot fields and the added counters. `{prefix}fact_lifecycle_lines`, doc id `<YYYY-MM-DD>`: the mark
that the day's ops line was claimed. Neither collection has a TTL policy and no field is named `expires_at`.
"""
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.fact_lifecycle_day import COUNTER_FIELDS, SNAPSHOT_FIELDS, FactLifecycleDay, day_doc_id
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..utils.logger import logger


class FirestoreFactLifecycleCounters(FactLifecycleCountersPort):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._daily = db_client.collection(env_config.fact_lifecycle_daily_collection)
        self._lines = db_client.collection(env_config.fact_lifecycle_lines_collection)

    @staticmethod
    def _check(account_id: str, day: date) -> None:
        if not account_id:
            raise ValueError("account_id is required for the fact lifecycle counters")
        if isinstance(day, datetime):
            raise ValueError("day must be a date, not a datetime")

    async def record_sweep(self, account_id: str, day: date, values: Mapping[str, Any]) -> None:
        self._check(account_id, day)
        unknown = sorted(set(values) - SNAPSHOT_FIELDS)
        if unknown:
            raise ValueError(f"not snapshot fields: {unknown}")
        data: Dict[str, Any] = {"account_id": account_id, "day": day.isoformat(), **values}
        await self._daily.document(day_doc_id(account_id, day)).set(data, merge=True)

    async def add(self, account_id: str, day: date, counts: Mapping[str, int]) -> None:
        self._check(account_id, day)
        unknown = sorted(set(counts) - COUNTER_FIELDS)
        if unknown:
            raise ValueError(f"not counter fields: {unknown}")
        for name, value in counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"count {name} must be a non-negative integer")
        added = {name: value for name, value in counts.items() if value}
        if not added:
            return
        data: Dict[str, Any] = {"account_id": account_id, "day": day.isoformat()}
        data.update({name: firestore.Increment(value) for name, value in added.items()})
        await self._daily.document(day_doc_id(account_id, day)).set(data, merge=True)

    async def for_day(self, day: date) -> List[FactLifecycleDay]:
        rows: List[FactLifecycleDay] = []
        async for snap in self._daily.where(filter=FieldFilter("day", "==", day.isoformat())).stream():
            try:
                rows.append(FactLifecycleDay.from_document(snap.to_dict() or {}))
            except (KeyError, ValueError):
                logger.warning(f"⚠️ [FactLifecycleCounters] unreadable row {snap.id[:8]} skipped")
        return sorted(rows, key=lambda row: row.account_id)

    async def claim_line(self, day: date) -> bool:
        try:
            await self._lines.document(day.isoformat()).create({"day": day.isoformat(), "claimed_at": datetime.now(timezone.utc)})
        except AlreadyExists:
            return False
        return True
