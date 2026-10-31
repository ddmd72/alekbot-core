"""Search v2 budgets in tests (USER_DRIVE_INDEXING_RFC §25.4.5): model dollars per class in the month's usage document
and the one-time initial build, which replaced the sync state's page and image counters (§24.7). The helpers write
and read them through the repository port, as the tick does."""
from __future__ import annotations

from datetime import datetime

from src.domain.drive_enrichment import InitialBuild, UsageClass
from src.domain.drive_index_planner import month_key
from src.domain.drive_usage import initial_build_doc_id
from src.ports.drive_index_repository import DriveIndexRepository

from .drive_index_fakes import legacy_account


async def spend(repo: DriveIndexRepository, cls: UsageClass, usd: float, now: datetime, *, user: str = "u") -> None:
    """`usd` already spent this month on `cls` (one call)."""
    await repo.add_model_usage(user, month_key(now), now.date().isoformat(), cls.value, usd, 0.0, 1)


async def close_initial_build(repo: DriveIndexRepository, now: datetime, *, user: str = "u") -> None:
    """The initial build is over: only the month's caps pay from now on. The build of the single-drive harness's
    connection account (`legacy_account`, MULTI_DRIVE_RFC §10)."""
    account = legacy_account(user)
    await repo.close_initial_build(user, account.realm, account.subject, now)


async def initial_build(repo: DriveIndexRepository, *, user: str = "u") -> InitialBuild:
    """The initial build of the single-drive harness's connection account."""
    account = legacy_account(user)
    return await repo.get_initial_build(user, account.realm, account.subject)


def initial_key(user: str = "u") -> str:
    """The in-memory repository's key of that build (`InMemoryDriveIndexRepository.initial`)."""
    account = legacy_account(user)
    return initial_build_doc_id(user, account.realm, account.subject)


async def month_usd(repo: DriveIndexRepository, cls: UsageClass, month: str, *, user: str = "u") -> float:
    usage = await repo.get_model_usage(user, month)
    return float(getattr(usage, f"{cls.value}_usd"))


async def month_calls(repo: DriveIndexRepository, month: str, *, user: str = "u") -> int:
    return (await repo.get_model_usage(user, month)).calls


def affordable(usd: float, unit_usd: float) -> int:
    """Units (pages, images) `usd` pays for at the per-unit estimate, as the tick counts them."""
    return int(usd / unit_usd + 1e-9)
