"""Facts S3 · T7: `BillingAccount.owner_user_ids` reads the IAM policy and nothing else (plan §3.10, R1-F8)."""
from src.domain.billing import BillingAccount
from src.ports.iam_port import Role


def test_the_owners_are_the_users_with_the_owner_role_sorted():
    account = BillingAccount(iam_policy={"u-z": "owner", "u-a": "owner", "u-m": "member", "u-v": "viewer"})

    assert account.owner_user_ids() == ["u-a", "u-z"]


def test_the_role_value_is_the_one_the_iam_port_stores():
    account = BillingAccount(iam_policy={"u1": Role.OWNER.value, "u2": Role.MEMBER.value})

    assert account.owner_user_ids() == ["u1"]


def test_no_owner_gives_an_empty_list():
    assert BillingAccount(iam_policy={"u1": "member"}).owner_user_ids() == []
    assert BillingAccount().owner_user_ids() == []


def test_the_answer_is_a_fresh_list_and_leaves_the_policy_alone():
    account = BillingAccount(iam_policy={"u1": "owner"})

    account.owner_user_ids().append("intruder")

    assert account.owner_user_ids() == ["u1"]
    assert account.iam_policy == {"u1": "owner"}
