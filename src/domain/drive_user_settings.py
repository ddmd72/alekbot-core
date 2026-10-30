"""A user's drive settings that are not one connection's (MULTI_DRIVE_RFC §4.4, E2).

`primary_connection_id`: the connection that receives the Inbox and deep-research copies (D7). Stored at
`{prefix}drive_user_settings/{user_id}`, read fresh on every use and written only by the drive connection service.
Invariant I2: the primary is ACTIVE and has a write folder (stage 1: the area root). None until the first connection.
"""
from dataclasses import dataclass
from typing import Optional

from .connection_scope import check_connection_id


@dataclass(frozen=True)
class DriveUserSettings:
    user_id: str
    primary_connection_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.user_id:
            raise ValueError("DriveUserSettings needs a user_id")
        if self.primary_connection_id is not None:
            check_connection_id(self.primary_connection_id)
