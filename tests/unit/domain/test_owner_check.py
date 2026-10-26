"""Facts S4 T8a: `not_owner_reason`, whether a user may act as the account's owner (the memory review's owner check)."""
from src.domain.billing import BillingAccount
from src.domain.owner_timezone import not_owner_reason
from src.domain.user import UserProfile

ACCOUNT = "account-abcdef123456"


def held(**roles: str) -> BillingAccount:
    return BillingAccount(account_id=ACCOUNT, iam_policy=dict(roles))


def profile(user_id: str, account_id: str = ACCOUNT) -> UserProfile:
    return UserProfile(user_id=user_id, account_id=account_id)


def test_the_first_owner_with_a_profile_of_the_account_may_act():
    assert not_owner_reason(held(u1="owner", u2="member"), "u1", profile("u1")) is None


def test_an_owner_who_is_not_the_first_may_not():
    assert not_owner_reason(held(u3="owner", u1="owner"), "u3", profile("u3")) == "user is not the account's first owner"


def test_a_member_may_not():
    assert not_owner_reason(held(u1="owner", u2="member"), "u2", profile("u2")) == "user is not the account's first owner"


def test_a_missing_account_or_an_account_with_no_owner_says_why():
    assert not_owner_reason(None, "u1", profile("u1")) == "account not found"
    assert not_owner_reason(held(u1="member"), "u1", profile("u1")) == "account has no owner"


def test_the_owner_needs_a_profile_of_this_account():
    assert not_owner_reason(held(u1="owner"), "u1", None) == "user has no profile"
    assert not_owner_reason(held(u1="owner"), "u1", profile("u1", "account-other")) == "user's profile belongs to another account"
    assert not_owner_reason(held(u1="owner"), "u1", UserProfile(user_id="u1")) == "user's profile belongs to another account"
