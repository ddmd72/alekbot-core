"""Facts S3 final review L3: the owner-timezone rule both the consolidation run and the dry-run kit call."""
from typing import Dict, List, Optional

from src.domain.billing import BillingAccount
from src.domain.owner_timezone import FALLBACK_TIMEZONE, resolve_owner_timezone
from src.domain.user import UserBotConfig, UserProfile

ACCOUNT = "account-abcdef123456"


def profile(user_id: str, tz: str) -> UserProfile:
    return UserProfile(user_id=user_id, account_id=ACCOUNT, config=UserBotConfig(timezone=tz))


class Store:
    """Holds accounts and profiles, records every id read, raises for the ids it was told to."""

    def __init__(self, accounts: List[BillingAccount], users: List[UserProfile], raises: Optional[Dict[str, Exception]] = None):
        self.accounts = {a.account_id: a for a in accounts}
        self.users = {u.user_id: u for u in users}
        self.raises = raises or {}
        self.read: List[str] = []

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        self.read.append(account_id)
        if account_id in self.raises:
            raise self.raises[account_id]
        return self.accounts.get(account_id)

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        self.read.append(user_id)
        if user_id in self.raises:
            raise self.raises[user_id]
        return self.users.get(user_id)


async def resolve(store: Store):
    return await resolve_owner_timezone(ACCOUNT, store.get_account, store.get_user)


def held(**roles: str) -> BillingAccount:
    return BillingAccount(account_id=ACCOUNT, iam_policy=dict(roles))


async def test_the_zone_is_the_first_owners_and_a_member_is_never_asked():
    store = Store([held(zed="owner", ann="owner", bob="member")], [profile("ann", "Asia/Tokyo"), profile("bob", "Europe/Paris")])

    resolved = await resolve(store)

    assert (resolved.zone, resolved.unresolved, resolved.owner_count) == ("Asia/Tokyo", None, 2)
    assert store.read == [ACCOUNT, "ann"]


async def test_each_missing_step_falls_back_to_utc_and_says_which():
    no_account = await resolve(Store([], []))
    no_owner = await resolve(Store([held(bob="member")], []))
    no_profile = await resolve(Store([held(ann="owner")], []))

    assert [r.zone for r in (no_account, no_owner, no_profile)] == [FALLBACK_TIMEZONE] * 3
    assert [r.unresolved for r in (no_account, no_owner, no_profile)] == [
        "account not found", "account has no owner", "owner has no profile"]
    assert [r.owner_count for r in (no_account, no_owner, no_profile)] == [0, 0, 1]


async def test_an_invalid_zone_falls_back_and_names_the_error():
    resolved = await resolve(Store([held(ann="owner")], [profile("ann", "Mars/Olympus")]))

    assert resolved.zone == "UTC" and resolved.unresolved.startswith("ZoneInfoNotFoundError: ")


async def test_a_failing_store_falls_back_with_the_exception_and_keeps_the_owner_count():
    down = await resolve(Store([held(ann="owner")], [], raises={ACCOUNT: RuntimeError("down")}))
    broken_profile = await resolve(Store([held(ann="owner", bob="owner")], [], raises={"ann": ValueError("bad")}))

    assert (down.zone, down.unresolved, down.owner_count) == ("UTC", "RuntimeError: down", 0)
    assert (broken_profile.zone, broken_profile.unresolved, broken_profile.owner_count) == ("UTC", "ValueError: bad", 2)
