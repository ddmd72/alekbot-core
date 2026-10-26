"""
FirestoreFactLifecycleCounters — FactLifecycleCountersPort on Firestore (FACT_LIFECYCLE_RFC D10).

`{prefix}fact_lifecycle_daily`, doc id `<account_id>:<YYYY-MM-DD>` (UTC day): `account_id`, `day` (ISO string), the
janitor's snapshot fields and the counters, each under its domain name. A snapshot is written as its whole field set,
merged, so every snapshot field is replaced and the counters stay; a counter is added with `Increment`. A missing or
unreadable number reads as 0. `{prefix}fact_lifecycle_lines`, doc id `<YYYY-MM-DD>`: the mark that the day's ops line
was claimed (`day` as an ISO string, `claimed_at` aware UTC). `{prefix}fact_lifecycle_run`, doc id `sweep`: the janitor's
run guard (`run_owner`, `run_until`, `acquired_at`), taken and freed in a transaction. No collection has a TTL policy and
no field is named `expires_at`.
"""
from dataclasses import asdict, fields
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.fact_lifecycle_day import CounterField, FactLifecycleDay, SweepSnapshot, check_count
from ..domain.fact_lifecycle_run_guard import run_guard_held
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..utils.logger import logger


RUN_GUARD_DOC = "sweep"


def _doc_id(account_id: str, day: date) -> str:
    return f"{account_id}:{day.isoformat()}"


def _count(value: Any) -> int:
    """A stored number as an int: missing or not a number reads as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


# The snapshot's numbers, as stored; with every counter, read the same way.
_SNAPSHOT_COUNTS = tuple(
    f.name for f in fields(SweepSnapshot) if f.name not in {"run_at", "breaker", "oldest_stale_days"}
)


def _day_from(data: Mapping[str, Any]) -> FactLifecycleDay:
    """A day row from its stored document; a missing or unreadable number reads as 0 (a NaN raises ValueError)."""
    numbers = {name: _count(data.get(name)) for name in _SNAPSHOT_COUNTS}
    numbers.update({field.value: _count(data.get(field.value)) for field in CounterField})
    return FactLifecycleDay(
        account_id=str(data.get("account_id", "")),
        day=date.fromisoformat(str(data["day"])),
        run_at=data.get("run_at") if isinstance(data.get("run_at"), datetime) else None,
        breaker=str(data.get("breaker") or ""),
        oldest_stale_days=_count(data["oldest_stale_days"]) if data.get("oldest_stale_days") is not None else None,
        **numbers,
    )


class FirestoreFactLifecycleCounters(FactLifecycleCountersPort):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._daily = db_client.collection(env_config.fact_lifecycle_daily_collection)
        self._lines = db_client.collection(env_config.fact_lifecycle_lines_collection)
        self._db = db_client
        self._run = db_client.collection(env_config.fact_lifecycle_run_collection).document(RUN_GUARD_DOC)

    @staticmethod
    def _check(account_id: str, day: date) -> None:
        if not account_id:
            raise ValueError("account_id is required for the fact lifecycle counters")
        if isinstance(day, datetime):
            raise ValueError("day must be a date, not a datetime")

    async def record_sweep(self, account_id: str, day: date, snapshot: SweepSnapshot) -> None:
        self._check(account_id, day)
        data: Dict[str, Any] = {"account_id": account_id, "day": day.isoformat(), **asdict(snapshot)}
        await self._daily.document(_doc_id(account_id, day)).set(data, merge=True)

    async def add(self, account_id: str, day: date, counts: Mapping[CounterField, int]) -> None:
        self._check(account_id, day)
        added: Dict[str, int] = {}
        for name, value in counts.items():
            field = CounterField(name)
            check_count(field.value, value)
            if value:
                added[field.value] = value
        if not added:
            return
        data: Dict[str, Any] = {"account_id": account_id, "day": day.isoformat()}
        data.update({name: firestore.Increment(value) for name, value in added.items()})
        await self._daily.document(_doc_id(account_id, day)).set(data, merge=True)

    async def for_day(self, day: date) -> List[FactLifecycleDay]:
        rows: List[FactLifecycleDay] = []
        async for snap in self._daily.where(filter=FieldFilter("day", "==", day.isoformat())).stream():
            try:
                rows.append(_day_from(snap.to_dict() or {}))
            except (KeyError, ValueError):
                logger.warning(f"⚠️ [FactLifecycleCounters] unreadable row {snap.id[:8]} skipped")
        return sorted(rows, key=lambda row: row.account_id)

    async def claim_line(self, day: date) -> bool:
        try:
            await self._lines.document(day.isoformat()).create({"day": day.isoformat(), "claimed_at": datetime.now(timezone.utc)})
        except AlreadyExists:
            return False
        return True

    async def acquire_run(self, owner: str, now: datetime, until: datetime) -> bool:
        if not owner:
            raise ValueError("owner is required to take the janitor's run guard")
        if now.tzinfo is None or until.tzinfo is None:
            raise ValueError("now and until must be timezone-aware")
        if until <= now:
            raise ValueError("until must be later than now")
        ref = self._run

        @firestore.async_transactional
        async def _take(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            held_until = (snap.to_dict() or {}).get("run_until") if snap.exists else None
            if run_guard_held(held_until, now):
                return False
            transaction.set(ref, {"run_owner": owner, "run_until": until, "acquired_at": now})
            return True

        taken = await _take(self._db.transaction())
        if not taken:
            logger.info("[FactLifecycleCounters] the run guard is held; this run is refused")
        return taken

    async def release_run(self, owner: str) -> None:
        ref = self._run

        @firestore.async_transactional
        async def _release(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return True
            if (snap.to_dict() or {}).get("run_owner") != owner:
                return False
            transaction.delete(ref)
            return True

        if not await _release(self._db.transaction()):
            logger.warning("⚠️ [FactLifecycleCounters] release found another owner; its own guard had expired and was taken over")
