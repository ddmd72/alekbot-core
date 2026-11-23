"""SoftAuthenticator — a software WebAuthn platform authenticator for tests (CABINET_DEVICE_SESSIONS_RFC §7.3).

It answers the options the relying party sends exactly as a browser with a passkey would: `register(options)` makes a
new ES256 key pair (`cryptography`) and returns the `navigator.credentials.create` result as JSON (a `none`
attestation; authenticator data and clientDataJSON built by hand, CBOR by the minimal encoder below, so the test does
not lean on the library it tests); `sign_in(options)` returns the `navigator.credentials.get` result for one of its
credentials, signed with its private key. Flags and the counter are knobs, so a test can produce a response a real
authenticator would not (no user verification, a counter that went back, another user handle, another RP or origin).

A synced passkey (iCloud Keychain, Google Password Manager) keeps no counter: `counter_step=0` reports 0 every time.
"""
import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def cbor(value: Any) -> bytes:
    """The CBOR of ints, bytes, text and maps (what a COSE key and a `none` attestation object need)."""
    def head(major: int, n: int) -> bytes:
        if n < 24:
            return bytes([major << 5 | n])
        for limit, extra, fmt in ((0x100, 24, ">B"), (0x10000, 25, ">H"), (0x100000000, 26, ">I")):
            if n < limit:
                return bytes([major << 5 | extra]) + struct.pack(fmt, n)
        raise ValueError("value too large for this encoder")

    if isinstance(value, bool):
        raise TypeError("booleans are not needed here")
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return head(3, len(raw)) + raw
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(f"cannot encode {type(value).__name__}")


@dataclass
class Credential:
    credential_id: bytes
    key: ec.EllipticCurvePrivateKey
    user_handle: bytes
    counter: int = 0

    def cose_public_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})


class SoftAuthenticator:

    def __init__(self, rp_id: str, origin: str, *, backup_eligible: bool = False, counter_step: int = 1) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.backup_eligible = backup_eligible
        self.counter_step = counter_step
        self.credentials: List[Credential] = []

    def _flags(self, *, user_verified: bool, attested: bool) -> int:
        flags = UP | (UV if user_verified else 0) | (AT if attested else 0)
        return flags | (BE | BS if self.backup_eligible else 0)

    def _client_data(self, kind: str, options: Dict[str, Any], origin: Optional[str]) -> bytes:
        return json.dumps({"type": kind, "challenge": options["challenge"], "origin": origin or self.origin,
                           "crossOrigin": False}).encode("utf-8")

    def register(self, options: Dict[str, Any], *, user_verified: bool = True, origin: Optional[str] = None,
                 rp_id: Optional[str] = None, reuse: Optional[Credential] = None) -> Dict[str, Any]:
        """The `navigator.credentials.create` result for `options`; `reuse` registers an existing credential again."""
        cred = reuse or Credential(credential_id=os.urandom(16), key=ec.generate_private_key(ec.SECP256R1()),
                                   user_handle=unb64u(options["user"]["id"]))
        if reuse is None:
            self.credentials.append(cred)
        attested = (b"\x00" * 16 + struct.pack(">H", len(cred.credential_id)) + cred.credential_id
                    + cred.cose_public_key())
        auth_data = (hashlib.sha256((rp_id or self.rp_id).encode("utf-8")).digest()
                     + bytes([self._flags(user_verified=user_verified, attested=True)])
                     + struct.pack(">I", cred.counter) + attested)
        attestation = cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = self._client_data("webauthn.create", options, origin)
        return {"id": b64u(cred.credential_id), "rawId": b64u(cred.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client_data), "attestationObject": b64u(attestation),
                             "transports": ["internal"]},
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}

    def sign_in(self, options: Dict[str, Any], cred: Optional[Credential] = None, *, user_verified: bool = True,
                counter: Optional[int] = None, user_handle: Optional[bytes] = b"", origin: Optional[str] = None,
                signer: Optional[ec.EllipticCurvePrivateKey] = None) -> Dict[str, Any]:
        """The `navigator.credentials.get` result for `options` from `cred` (the last registered by default).
        `counter` overrides the advanced counter; `user_handle=None` omits it, b"" (the default) sends the
        credential's own; `signer` signs with another key."""
        cred = cred or self.credentials[-1]
        if counter is None:
            cred.counter += self.counter_step
            counter = cred.counter
        auth_data = (hashlib.sha256(self.rp_id.encode("utf-8")).digest()
                     + bytes([self._flags(user_verified=user_verified, attested=False)]) + struct.pack(">I", counter))
        client_data = self._client_data("webauthn.get", options, origin)
        signature = (signer or cred.key).sign(auth_data + hashlib.sha256(client_data).digest(),
                                              ec.ECDSA(hashes.SHA256()))
        response: Dict[str, Any] = {"clientDataJSON": b64u(client_data), "authenticatorData": b64u(auth_data),
                                    "signature": b64u(signature)}
        if user_handle is not None:
            response["userHandle"] = b64u(user_handle or cred.user_handle)
        return {"id": b64u(cred.credential_id), "rawId": b64u(cred.credential_id), "type": "public-key",
                "response": response, "clientExtensionResults": {}, "authenticatorAttachment": "platform"}
