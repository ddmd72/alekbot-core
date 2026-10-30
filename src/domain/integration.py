"""The integration a credential serves (MULTI_DRIVE_RFC §3, §4.1): the Cabinet groups credentials by it.

Not a kind: one integration can have several kinds (`TASKS` has Google Tasks and To Do), so a consumer that needs its
own credential filters by the kind of its injected `ConnectorSpec`, never by integration (E25). The value is stored as
the plaintext field `integration` of a credential record.

R1's reconnect notices (`reconnect_policy`) key on it too: MAIL is Gmail, TASKS is To Do there (its tasks pass covers
To Do users only), DRIVE the drive.
"""
from enum import Enum


class Integration(str, Enum):
    MAIL = "mail"
    TASKS = "tasks"
    DRIVE = "drive"
