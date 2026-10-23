"""Facts S3 final review L3/R1: the pure owner-timezone decisions both the consolidation run and the dry-run kit make."""
from src.domain.billing import BillingAccount
from src.domain.owner_timezone import FALLBACK_TIMEZONE, failure_reason, owner_to_read, owner_zone
from src.domain.user import UserBotConfig, UserProfile

ACCOUNT = "account-abcdef123456"


def profile(user_id: str, tz: str) -> UserProfile:
    return UserProfile(user_id=user_id, account_id=ACCOUNT, config=UserBotConfig(timezone=tz))


def held(**roles: str) -> BillingAccount:
    return BillingAccount(account_id=ACCOUNT, iam_policy=dict(roles))


def test_the_first_owner_by_id_is_read_and_a_member_never_is():
    step = owner_to_read(held(zed="owner", ann="owner", bob="member"))

    assert (step.owner_id, step.owner_count, step.unresolved) == ("ann", 2, None)


def test_a_missing_account_or_owner_is_unresolved_and_names_no_owner_to_read():
    no_account = owner_to_read(None)
    no_owner = owner_to_read(held(bob="member"))

    assert (no_account.owner_id, no_account.owner_count, no_account.unresolved) == (None, 0, "account not found")
    assert (no_owner.owner_id, no_owner.owner_count, no_owner.unresolved) == (None, 0, "account has no owner")


def test_the_zone_is_the_owners_profile_zone():
    resolved = owner_zone(profile("ann", "Asia/Tokyo"))

    assert (resolved.zone, resolved.unresolved) == ("Asia/Tokyo", None)


def test_a_missing_profile_falls_back_to_utc_and_says_so():
    resolved = owner_zone(None)

    assert (resolved.zone, resolved.unresolved) == (FALLBACK_TIMEZONE, "owner has no profile") and FALLBACK_TIMEZONE == "UTC"


def test_an_invalid_zone_falls_back_to_utc_and_names_the_error():
    resolved = owner_zone(profile("ann", "Mars/Olympus"))

    assert resolved.zone == "UTC" and resolved.unresolved.startswith("ZoneInfoNotFoundError: ")


def test_a_failure_is_told_as_type_and_message():
    assert failure_reason(RuntimeError("down")) == "RuntimeError: down"
