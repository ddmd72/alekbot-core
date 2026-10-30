"""The state of a drive connection (MULTI_DRIVE_RFC §4.4).

- `ACTIVE`: in use.
- `NEEDS_RECONNECT`: the provider rejected the grant; a successful consent of the same account returns it to ACTIVE.
- `DISCONNECTED`: ended by the user. Terminal: the document and its tag stay reserved forever, so an old ref never
  resolves to a newer connection; a later connect of the same account is a new connection.

`can_transition(current, to)` is the one rule every store applies before writing a state: a move to the state already
held is no transition, and nothing leaves DISCONNECTED.
"""
from enum import Enum


class ConnectionState(str, Enum):
    ACTIVE = "active"
    NEEDS_RECONNECT = "needs_reconnect"
    DISCONNECTED = "disconnected"


_ALLOWED = frozenset({
    (ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT),
    (ConnectionState.NEEDS_RECONNECT, ConnectionState.ACTIVE),
    (ConnectionState.ACTIVE, ConnectionState.DISCONNECTED),
    (ConnectionState.NEEDS_RECONNECT, ConnectionState.DISCONNECTED),
})


def can_transition(current: ConnectionState, to: ConnectionState) -> bool:
    return (current, to) in _ALLOWED
