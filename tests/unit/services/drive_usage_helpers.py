"""Search v2 budgets in tests (USER_DRIVE_INDEXING_RFC §25.4.5): model dollars per class in the month's usage document
and the one-time initial build, which replaced the sync state's page and image counters (§24.7). The helpers write
and read them through the repository port, as the tick does."""
from __future__ import annotations

from datetime import datetime

from src.domain.drive_enrichment import UsageClass
from src.domain.drive_index_planner import month_key
from src.ports.drive_index_repository import DriveIndexRepository


async def spend(repo: DriveIndexRepository, cls: UsageClass, usd: float, now: datetime, *, user: str = "u") -> None:
    """`usd` already spent this month on `cls` (one call)."""
    await repo.add_model_usage(user, month_key(now), now.date().isoformat(), cls.value, usd, 0.0, 1)


async def close_initial_build(repo: DriveIndexRepository, now: datetime, *, user: str = "u") -> None:
    """The initial build is over: only the month's caps pay from now on."""
    await repo.close_initial_build(user, now)


async def month_usd(repo: DriveIndexRepository, cls: UsageClass, month: str, *, user: str = "u") -> float:
    usage = await repo.get_model_usage(user, month)
    return float(getattr(usage, f"{cls.value}_usd"))


async def month_calls(repo: DriveIndexRepository, month: str, *, user: str = "u") -> int:
    return (await repo.get_model_usage(user, month)).calls


def affordable(usd: float, unit_usd: float) -> int:
    """Units (pages, images) `usd` pays for at the per-unit estimate, as the tick counts them."""
    return int(usd / unit_usd + 1e-9)
