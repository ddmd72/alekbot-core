"""A drive ref resolved to its connection (MULTI_DRIVE_RFC §9): what `DriveConnectionService.resolve_ref` returns for a
tagged ref whose tag is one of the user's live (ACTIVE or NEEDS_RECONNECT) connections."""
from dataclasses import dataclass

from .drive_connection import DriveConnection
from .drive_handle import DriveHandle


@dataclass(frozen=True)
class ResolvedDriveRef:
    connection: DriveConnection
    item_id: str

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("a resolved ref needs an item id")

    @property
    def handle(self) -> DriveHandle:
        return self.connection.handle
