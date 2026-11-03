"""What the drive MCP connector may read on one drive (DRIVE_MCP_CONNECTOR_RFC §7.1). Pure.

A value is either the whole drive (everything its search scope covers) or a non-empty set of folders inside that scope.
"Nothing" is not a value: it is `None` on `DriveConnection.connector_access`, the default and the reading of every
document stored before the field. An empty folder set is never a value, which closes the fail-open trap of
`scope_folder_ids` (an empty scope folder list means "everything" on an AREA feed).

Public interface:
    ConnectorAccess (frozen)
    ConnectorAccess.whole_drive() -> ConnectorAccess
    ConnectorAccess.folders(ids) -> ConnectorAccess        ValueError on an empty set or an empty id
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple


@dataclass(frozen=True)
class ConnectorAccess:
    whole: bool = False
    folder_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.folder_ids, tuple):
            raise ValueError("connector folder ids are a tuple")
        if not all(isinstance(f, str) and f for f in self.folder_ids):
            raise ValueError("a connector folder id is a non-empty string")
        if self.whole == bool(self.folder_ids):
            raise ValueError("connector access is the whole drive or a non-empty set of folders, exactly one")

    @classmethod
    def whole_drive(cls) -> "ConnectorAccess":
        return cls(whole=True)

    @classmethod
    def folders(cls, ids: Sequence[str]) -> "ConnectorAccess":
        """The folders `ids`, de-duplicated with their order kept. ValueError when none is given."""
        if isinstance(ids, str):
            raise ValueError("connector folder ids are a sequence of ids, not one string")
        return cls(folder_ids=tuple(dict.fromkeys(ids)))
