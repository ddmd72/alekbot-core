"""The account owner's IANA zone: the one rule the consolidation run and the dry-run kit both read."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .billing import BillingAccount
    from .user import UserProfile

FALLBACK_TIMEZONE = "UTC"


@dataclass(frozen=True)
class OwnerTimezone:
    """`zone` is the owner's zone, or `FALLBACK_TIMEZONE` with `unresolved` saying why. `owner_count` is how many owners
    the account had when it was read (0 until an account was found)."""

    zone: str
    unresolved: Optional[str] = None
    owner_count: int = 0


async def resolve_owner_timezone(
    account_id: str,
    get_account: "Callable[[str], Awaitable[Optional[BillingAccount]]]",
    get_user: "Callable[[str], Awaitable[Optional[UserProfile]]]",
) -> OwnerTimezone:
    """The first owner's profile zone; a missing account, owner or profile, an invalid zone or a failing read gives UTC
    and the reason. The account comes from the profile only and the zone never decides it."""
    owner_count = 0
    try:
        account = await get_account(account_id)
        if account is None:
            return OwnerTimezone(FALLBACK_TIMEZONE, "account not found")
        owners = account.owner_user_ids()
        owner_count = len(owners)
        if not owners:
            return OwnerTimezone(FALLBACK_TIMEZONE, "account has no owner")
        owner = await get_user(owners[0])
        if owner is None:
            return OwnerTimezone(FALLBACK_TIMEZONE, "owner has no profile", owner_count)
        zone = owner.config.timezone
        ZoneInfo(zone)
        return OwnerTimezone(zone, None, owner_count)
    except Exception as exc:
        return OwnerTimezone(FALLBACK_TIMEZONE, f"{type(exc).__name__}: {exc}", owner_count)
