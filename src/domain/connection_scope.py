"""The scope every connection-scoped drive key is built from (MULTI_DRIVE_RFC §8, E8).

`ConnectionScope(user_id, connection_id)`: index records, chunks, hints, transcript jobs and batches, the sync state and
the `drive_sync` task keys are keyed `{connection_id}:…`, with `user_id` a stored field that a read checks (I3). A
connection id is random and globally unique, and it holds neither `:` nor `/`, so `{connection_id}:{rest}` never
collides across connections, whatever the rest holds.
"""
from dataclasses import dataclass

_FORBIDDEN_IN_CONNECTION_ID = (":", "/")


@dataclass(frozen=True)
class ConnectionScope:
    user_id: str
    connection_id: str

    def __post_init__(self) -> None:
        if not self.user_id:
            raise ValueError("ConnectionScope needs a user_id")
        check_connection_id(self.connection_id)


def check_connection_id(connection_id: str) -> None:
    """A connection id is non-empty and holds no `:` (the key separator) and no `/` (a document path separator).
    Raises ValueError otherwise."""
    if not connection_id:
        raise ValueError("a connection id must not be empty")
    if any(ch in connection_id for ch in _FORBIDDEN_IN_CONNECTION_ID):
        raise ValueError(f"a connection id holds no ':' or '/': {connection_id!r}")
