"""Passkey — one registered sign-in credential of a user (CABINET_DEVICE_SESSIONS_RFC §7.3).

A passkey is a key pair the user's keychain holds (synced across their devices, F2); the record keeps what verifying
its next assertion needs: the public key, its algorithm and the signature counter. It is not a device: the device is
the session (§6). `label` and `platform_hint` come from the User-Agent of the session that registered it: untrusted
text, shown literally and never obeyed. The record is addressed by `cred_hash`, the SHA-256 hex of the credential id
(`credential_hash`), which is also its Firestore doc id.

This module holds the record, its `PasskeyKind` and the two pure rules every store applies (the doc id and the
counter rule) on purpose: they have no meaning apart from it.
"""
import hashlib
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PasskeyKind(str, Enum):
    """Which verifier the key belongs to: a WebAuthn passkey (the only kind; §12 names a later second one)."""
    WEBAUTHN = "webauthn"


def credential_hash(credential_id: bytes) -> str:
    """The record's address: SHA-256 hex of the raw credential id."""
    return hashlib.sha256(credential_id).hexdigest()


def sign_count_advances(stored: int, presented: int) -> bool:
    """Whether an assertion's signature counter may replace the stored one (WebAuthn L3 §7.2 step 21): an authenticator
    that keeps no counter reports 0 every time (synced passkeys do), which is accepted while the stored counter is 0
    too; once either is nonzero, the presented one must be strictly greater, otherwise the credential may be cloned or
    the assertion replayed."""
    if stored == 0 and presented == 0:
        return True
    return presented > stored


class Passkey(BaseModel):
    """One passkey (frozen; change it with `model_copy(update=...)`). Every datetime is tz-aware UTC.

    `credential_id` is the raw credential id (not a secret: the browser hands it to anyone who asks the
    authenticator; kept so a new registration can exclude the user's existing ones). `public_key` is the COSE-encoded
    key and `algorithm` its COSE algorithm id. `backup_eligible` is the BE flag, fixed at registration (F3).
    `last_used_at` is None until the first sign-in with it; `revoked_at` is None while it is not revoked."""

    model_config = ConfigDict(frozen=True)

    cred_hash: str
    credential_id: bytes
    user_id: str
    account_id: str
    kind: PasskeyKind
    public_key: bytes
    algorithm: int
    sign_count: int
    label: str
    platform_hint: str
    backup_eligible: bool
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
