"""The values of a passkey ceremony (CABINET_DEVICE_SESSIONS_RFC §7.3): what `SignerVerifierPort` returns after it
verified a registration or an assertion, what `PasskeyService` hands the caller to start one, and the one refusal.

The browser's side of a ceremony stays a JSON dict (the options sent to `navigator.credentials.create/get` and the
credential it returns): only the verifier reads its inside, so a second verifier (§12) can define its own.

This module holds several public classes on purpose: they are one vocabulary, the port's and the service's.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class PasskeyRegistration:
    """A verified registration: the new credential's raw id, its COSE public key and algorithm, the counter it started
    at, and its BE flag (F3). User verification was required and held."""
    credential_id: bytes
    public_key: bytes
    algorithm: int
    sign_count: int
    backup_eligible: bool


@dataclass(frozen=True)
class PasskeyAssertion:
    """A verified assertion: the credential's raw id, the counter it reported, and the user handle the authenticator
    returned (the `user.id` given at registration; None when it returned none). User verification was required and
    held; the signature matched the stored key."""
    credential_id: bytes
    sign_count: int
    user_handle: Optional[bytes]


@dataclass(frozen=True)
class CeremonyStart:
    """The start of a ceremony: the nonce the caller keeps (a short-lived httponly cookie, §7.3) and presents to finish
    it, and the options the browser passes to WebAuthn."""
    nonce: str = field(repr=False)
    options: Dict[str, Any]


class PasskeyRejected(Exception):
    """A ceremony refused: an unknown, expired or replayed challenge, a response that does not verify, an unknown or
    revoked passkey, or a counter that did not advance. The message is for the log, never for the browser."""
