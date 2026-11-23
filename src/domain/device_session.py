"""DeviceSession — one signed-in device of the Cabinet, revocable server-side (CABINET_DEVICE_SESSIONS_RFC §7.1).

The record behind a device's refresh cookie: who owns it, how and when it was signed in, the rotating refresh secret
(only its SHA-256 is stored, `refresh_secret_hash` in `device_session_policy`), and its revocation. The lifetimes that
read these fields are `DeviceSessionPolicy`'s. `label` and `platform_hint` come from the User-Agent: untrusted text,
shown literally and never obeyed.

This module holds the record and the two enums of its fields (`SessionAuthKind`, `SessionRevokeReason`) on purpose:
they have no meaning apart from it.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class SessionAuthKind(str, Enum):
    """How the session was signed in: a Google OAuth login, or a passkey assertion (§7.3)."""
    GOOGLE = "google"
    PASSKEY = "passkey"


class SessionRevokeReason(str, Enum):
    """Why a session was revoked: the owner's logout, a row of the devices list, the kill switch (`$devices off`), or
    a refresh secret presented after it had been rotated away (a replay)."""
    LOGOUT = "logout"
    DEVICE_LIST = "device_list"
    KILL_SWITCH = "kill_switch"
    REPLAY = "replay"


class DeviceSession(BaseModel):
    """One device session (frozen; change it with `model_copy(update=...)`). Every datetime is tz-aware UTC.

    `expires_at` is the absolute end (`created_at` + `DeviceSessionPolicy.ABSOLUTE_TTL`); `purge_after` is the
    Firestore TTL field (`last_seen_at` + `DeviceSessionPolicy.PURGE_AFTER`). `prev_refresh_hash` and `rotated_at` are
    None until the first rotation; `revoked_at` and `revoke_reason` are None while the session is not revoked.
    `passkey_at` is when a passkey assertion last proved this session (its passkey sign-in, or the step-up a call start
    asks for, §7.3); None until one did. A call start reads it (`DeviceSessionPolicy.passkey_fresh`)."""

    model_config = ConfigDict(frozen=True)

    sid: str
    user_id: str
    account_id: str
    label: str
    platform_hint: str
    auth_kind: SessionAuthKind
    auth_at: datetime
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    refresh_hash: str
    prev_refresh_hash: Optional[str] = None
    rotated_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoke_reason: Optional[SessionRevokeReason] = None
    purge_after: datetime
    passkey_at: Optional[datetime] = None
