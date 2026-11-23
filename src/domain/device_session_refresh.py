"""RefreshResult — what one device-session refresh produced (CABINET_DEVICE_SESSIONS_RFC §7.1 "Refresh").

`ROTATE`: a new access token and a new refresh secret (the caller sets both cookies). `GRACE`: a new access token only
(the caller sets no refresh cookie, so racing tabs end with the newer one). `REPLAY` and `DEAD`: neither; the caller
refuses and clears the cookies. `session` is the record after the refresh's write (None when the sid is unknown).
"""
from dataclasses import dataclass, field
from typing import Optional

from .device_session import DeviceSession
from .device_session_policy import RefreshOutcome


@dataclass(frozen=True)
class RefreshResult:
    outcome: RefreshOutcome
    session: Optional[DeviceSession]
    access_token: Optional[str] = field(default=None, repr=False)
    refresh_secret: Optional[str] = field(default=None, repr=False)
