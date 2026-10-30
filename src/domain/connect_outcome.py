"""The result of finishing a server-side connect flow (MULTI_DRIVE_RFC §5.2).

`kind` is the connector kind of the flow (None when the flow is unknown). `status` is `connected` or the error:
`state` (unknown, expired or already consumed flow), `user` (the session user is not the flow's user), `scope`
(the granted scopes miss a required one), `account` (a reconnect came back with another provider account),
`exchange` (the provider edge raised). `return_path` is where the web layer sends the user next.
"""
from dataclasses import dataclass
from typing import Literal, Optional

ConnectStatus = Literal["connected", "state", "user", "scope", "account", "exchange"]


@dataclass(frozen=True)
class ConnectOutcome:
    kind: Optional[str]
    status: ConnectStatus
    return_path: str = "/cabinet"
