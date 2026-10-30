"""What a drive adapter is told about the connection a call is for (MULTI_DRIVE_RFC §3, §6, E6).

`DriveHandle(user_id, connection_id, credential_id, kind)` is slim on purpose: an adapter cannot read the mount name,
the access policy or the ref tag, so the inward direction of the hexagon is held by the type. The router dispatches on
`kind`; the adapter finds its grant by `credential_id` and keys its caches, locks and token caches by
`(user_id, credential_id)` or `connection_id`, never by `user_id` alone (T-3). Built from a `DriveConnection`
(`DriveConnection.handle`), never by hand outside tests.
"""
from dataclasses import dataclass

from .connection_scope import ConnectionScope, check_connection_id


@dataclass(frozen=True)
class DriveHandle:
    user_id: str
    connection_id: str
    credential_id: str
    kind: str

    def __post_init__(self) -> None:
        for name in ("user_id", "connection_id", "credential_id", "kind"):
            if not getattr(self, name):
                raise ValueError(f"DriveHandle needs a {name}")
        check_connection_id(self.connection_id)

    @property
    def scope(self) -> ConnectionScope:
        """The connection's key scope."""
        return ConnectionScope(user_id=self.user_id, connection_id=self.connection_id)
