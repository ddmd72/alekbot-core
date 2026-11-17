"""
Account roles.

`Role` is the stored value of `BillingAccount.iam_policy` entries (user_id -> role). Cabinet reads and the team
invite write the lowercase value.
"""
from enum import Enum


class Role(str, Enum):
    """
    Roles an account member can hold: OWNER (full control), MEMBER (read/write shared resources),
    VIEWER (read-only).
    """
    OWNER = "owner"
    MEMBER = "member"
    VIEWER = "viewer"
