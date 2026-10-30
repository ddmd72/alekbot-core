"""
Drive model usage — the arithmetic of the usage and initial-build documents (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§25.4.5, D27). Pure: no I/O, no clock; both repository adapters apply the same rules.

The documents live in `{prefix}drive_model_usage`, outside the sync state, so a purge keeps them:
    {user_id}:{YYYY-MM}   one month's dollars per class, the fallback's share, calls and a daily map; TTL 400 days
    {user_id}:initial     the one-time initial-build allowance (D27); no TTL, it never reopens

Public interface:
    MODEL_USAGE_TTL, INITIAL_DOC_SUFFIX
    usage_doc_id(user_id, month) / initial_doc_id(user_id)
    initial_build_doc_id(user_id, realm, subject)   the initial-build record by provider account (MULTI_DRIVE_RFC §10)
    usage_expires_at(month) -> datetime          the TTL timestamp of a month's document
    added_usage(usage, day, cls, usd, fallback_usd, calls, covered_usd=0) -> ModelUsageMonth
    reserved_field(cls) / reserved_usage(usage, cls, delta_usd)   a class's open reservations (transcription, Δ106)
    new_initial_build() -> InitialBuild          an unopened build with INITIAL_BUILD_USD
    spent_initial(build, usd, now) -> (InitialBuild, float)   the build after a spend and the dollars it covered
    closed_initial(build, now) -> InitialBuild
"""
from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

from .drive_enrichment import InitialBuild, ModelUsageMonth, UsageClass
from .drive_index_params import INITIAL_BUILD_USD
from .drive_transcript import safe_doc_id

MODEL_USAGE_TTL = timedelta(days=400)        # a month's usage document (§25.4.5)
INITIAL_DOC_SUFFIX = "initial"
_SPENT_EPS = 1e-9                            # a float allowance left below this is spent


def usage_doc_id(user_id: str, month: str) -> str:
    return f"{user_id}:{month}"


def initial_doc_id(user_id: str) -> str:
    return f"{user_id}:{INITIAL_DOC_SUFFIX}"


def initial_build_doc_id(user_id: str, realm: str, subject: str) -> str:
    """`safe_doc_id({user_id}:initial:{realm}:{subject})` (MULTI_DRIVE_RFC §10, D8): one initial build per provider
    account, so a reconnect (a new connection of the same account) finds the same record and opens no new budget.
    User-scoped by design; `initial_doc_id` is the single-drive key, kept until its callers move (slice 1b).
    ValueError for an empty realm or subject."""
    if not realm or not subject:
        raise ValueError("an initial-build key needs the account's realm and subject")
    return safe_doc_id(f"{user_id}:{INITIAL_DOC_SUFFIX}:{realm}:{subject}")


def usage_expires_at(month: str) -> datetime:
    """MODEL_USAGE_TTL after 00:00 UTC of the month's first day ("YYYY-MM")."""
    year, mon = (int(part) for part in month.split("-"))
    return datetime(year, mon, 1, tzinfo=timezone.utc) + MODEL_USAGE_TTL


def added_usage(usage: ModelUsageMonth, day: str, cls: str, usd: float, fallback_usd: float,
                calls: int, covered_usd: float = 0.0) -> ModelUsageMonth:
    """`usd` added to the class's dollars and to the day's total, `fallback_usd` to the month's and the day's fallback
    share, `calls` to the calls, `covered_usd` (the part of `usd` the initial build paid, D27) to the class's
    `<cls>_initial_usd`, which the month's cap does not count. `cls` must be a UsageClass value (ValueError
    otherwise)."""
    name = f"{UsageClass(cls).value}_usd"
    initial = f"{UsageClass(cls).value}_initial_usd"
    daily: Dict[str, Tuple[float, float]] = dict(usage.daily)
    if day:   # "" = no daily entry (a booking into a month other than the day's)
        total, fallback = daily.get(day, (0.0, 0.0))
        daily[day] = (total + usd, fallback + fallback_usd)
    return replace(usage, **{name: getattr(usage, name) + usd, initial: getattr(usage, initial) + covered_usd},
                   fallback_usd=usage.fallback_usd + fallback_usd, calls=usage.calls + calls, daily=daily)


def reserved_field(cls: str) -> str:
    """The usage field holding a class's open reservations (`<cls>_reserved_usd`). Only classes bought ahead of their
    cost (transcription batches, Δ106) have one; any other class is a ValueError."""
    name = f"{UsageClass(cls).value}_reserved_usd"
    if name not in {f.name for f in fields(ModelUsageMonth)}:
        raise ValueError(f"usage class {cls!r} has no reservations")
    return name


def reserved_usage(usage: ModelUsageMonth, cls: str, delta_usd: float) -> ModelUsageMonth:
    """`delta_usd` (negative releases) added to the class's open reservations in this month's document."""
    name = reserved_field(cls)
    return replace(usage, **{name: getattr(usage, name) + delta_usd})


def new_initial_build() -> InitialBuild:
    return InitialBuild(allowance_usd=INITIAL_BUILD_USD)


def spent_initial(build: InitialBuild, usd: float, now: datetime) -> Tuple[InitialBuild, float]:
    """Cover as much of `usd` as the allowance has left (D27): the build opens at `now` on its first covered spend and
    closes at `now` when the allowance is spent. A closed build, or a spend of no dollars, covers 0 and is returned
    unchanged."""
    if build.closed_at is not None or usd <= 0:
        return build, 0.0
    covered = min(usd, max(build.allowance_usd - build.spent_usd, 0.0))
    spent = build.spent_usd + covered
    closed = now if build.allowance_usd - spent <= _SPENT_EPS else None
    return replace(build, spent_usd=spent, opened_at=build.opened_at or now, closed_at=closed), covered


def closed_initial(build: InitialBuild, now: datetime) -> InitialBuild:
    """The build closed at `now`; an already closed build keeps its `closed_at` (it never reopens)."""
    return build if build.closed_at is not None else replace(build, closed_at=now)
