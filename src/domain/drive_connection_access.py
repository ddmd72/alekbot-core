"""The settings of one connection the access policy reads (MULTI_DRIVE_RFC §11.3): `drive_access_policy.decide`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, FrozenSet, Optional

from .drive_connector_access import ConnectorAccess

if TYPE_CHECKING:       # the connection module imports the policy (its `scope_key`): no runtime import back
    from .drive_connection import DriveConnection


@dataclass(frozen=True)
class ConnectionAccess:
    drive_name: str                       # the mount name, for the refusal lines
    root_item_id: str                     # the drive's root (`get_root`)
    scope_folder_ids: FrozenSet[str]
    scope_whole_drive: bool
    write_folder_id: Optional[str]        # None: read-only (D11); ROOT_FOLDER (""): the root; else a folder id
    connector_access: Optional[ConnectorAccess] = None   # DRIVE_MCP_CONNECTOR_RFC §7.1; None: the connector reads nothing

    @classmethod
    def of(cls, connection: "DriveConnection", root_item_id: str) -> "ConnectionAccess":
        return cls(drive_name=connection.mount_name, root_item_id=root_item_id,
                   scope_folder_ids=frozenset(connection.scope_folder_ids),
                   scope_whole_drive=connection.scope_whole_drive, write_folder_id=connection.write_folder_id,
                   connector_access=connection.connector_access)
