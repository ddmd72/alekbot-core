"""The account owner's IANA zone: the pure decisions over the two reads it takes.

The reads (the account, then the first owner's profile) are I/O and happen outside; each step below decides over what was
read, and `ResolvedOwnerZone` is the answer.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .billing import BillingAccount
    from .user import UserProfile

FALLBACK_TIMEZONE = "UTC"


@dataclass(frozen=True)
class OwnerToRead:
    """Step 1: whose profile to read. `owner_id` is None when the zone is unresolved, and `unresolved` says why."""

    owner_id: Optional[str]
    owner_count: int = 0
    unresolved: Optional[str] = None


@dataclass(frozen=True)
class OwnerZone:
    """Step 2: `zone` is the owner's zone, or `FALLBACK_TIMEZONE` with `unresolved` saying why."""

    zone: str
    unresolved: Optional[str] = None


@dataclass(frozen=True)
class ResolvedOwnerZone:
    """Whose zone it is and the zone. `owner_user_id` is the account's first owner, or None when the account or its owner
    is unknown; `zone` is that owner's IANA zone, or `FALLBACK_TIMEZONE` whenever it could not be resolved."""

    owner_user_id: Optional[str]
    zone: str


def owner_to_read(account: "Optional[BillingAccount]") -> OwnerToRead:
    """The first owner of the account (`owner_user_ids` is sorted); the account comes from the profile only and the zone
    never decides it."""
    if account is None:
        return OwnerToRead(None, 0, "account not found")
    owners = account.owner_user_ids()
    if not owners:
        return OwnerToRead(None, 0, "account has no owner")
    return OwnerToRead(owners[0], len(owners))


def owner_zone(owner: "Optional[UserProfile]") -> OwnerZone:
    """The zone of the owner's profile; a missing profile or a zone that is not an IANA zone gives UTC and the reason."""
    if owner is None:
        return OwnerZone(FALLBACK_TIMEZONE, "owner has no profile")
    zone = owner.config.timezone
    try:
        ZoneInfo(zone)
    except Exception as exc:
        return OwnerZone(FALLBACK_TIMEZONE, failure_reason(exc))
    return OwnerZone(zone)


def failure_reason(exc: BaseException) -> str:
    """How a failed read or an invalid zone is told in the unresolved warning."""
    return f"{type(exc).__name__}: {exc}"
