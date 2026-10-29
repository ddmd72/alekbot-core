"""An SDK-shaped fake of the Cloud KMS async client, for the boot and graph tests (secrets-at-rest plan D-P1).

`FakeKmsClient` answers `encrypt(request=...)` and `decrypt(request=...)` with real `google.cloud.kms` response
types, the way the service does: it checks the request's CRC32C fields (a mismatch raises, as the service answers
INVALID_ARGUMENT), sets the `verified_*` flags and the response CRC32C fields, and really encrypts with AES-GCM under
one key per key name with the AAD bound, so a wrong key name, a wrong AAD or a tampered ciphertext raises.

`install(monkeypatch)` replaces `google.cloud.kms.KeyManagementServiceAsyncClient` with the fake class (the adapter
looks the class up at its first call), so a graph built over it never reaches `google.auth.default`. Every client
built is in `FakeKmsClient.instances`.
"""
import os
from typing import Any, Dict, List

import google_crc32c
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.cloud import kms

_NONCE_BYTES = 12


def _crc(data: bytes) -> int:
    return int(google_crc32c.value(data))


class FakeKmsError(Exception):
    """What the fake raises where the service would answer an error (INVALID_ARGUMENT, FAILED_PRECONDITION)."""


class FakeKmsClient:
    """One fake service; keys are created on first use of a key name and live as long as the class."""

    instances: List["FakeKmsClient"] = []
    _keys: Dict[str, bytes] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.requests: List[Dict[str, Any]] = []
        FakeKmsClient.instances.append(self)

    @classmethod
    def _key(cls, name: str) -> AESGCM:
        return AESGCM(cls._keys.setdefault(name, os.urandom(32)))

    @staticmethod
    def _check_crc(request: Dict[str, Any], field: str, crc_field: str) -> None:
        if crc_field in request and request[crc_field] != _crc(request[field]):
            raise FakeKmsError(f"{crc_field} does not match {field}")

    async def encrypt(self, request: Dict[str, Any], **_: Any) -> kms.EncryptResponse:
        self.requests.append({"op": "encrypt", **request})
        self._check_crc(request, "plaintext", "plaintext_crc32c")
        self._check_crc(request, "additional_authenticated_data", "additional_authenticated_data_crc32c")
        nonce = os.urandom(_NONCE_BYTES)
        sealed = nonce + self._key(request["name"]).encrypt(
            nonce, request["plaintext"], request.get("additional_authenticated_data", b""))
        return kms.EncryptResponse(
            name=request["name"] + "/cryptoKeyVersions/1", ciphertext=sealed, ciphertext_crc32c=_crc(sealed),
            verified_plaintext_crc32c="plaintext_crc32c" in request,
            verified_additional_authenticated_data_crc32c="additional_authenticated_data_crc32c" in request)

    async def decrypt(self, request: Dict[str, Any], **_: Any) -> kms.DecryptResponse:
        self.requests.append({"op": "decrypt", **request})
        self._check_crc(request, "ciphertext", "ciphertext_crc32c")
        self._check_crc(request, "additional_authenticated_data", "additional_authenticated_data_crc32c")
        raw = request["ciphertext"]
        try:
            plaintext = self._key(request["name"]).decrypt(
                raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], request.get("additional_authenticated_data", b""))
        except Exception as e:
            raise FakeKmsError("decryption failed") from e
        return kms.DecryptResponse(plaintext=plaintext, plaintext_crc32c=_crc(plaintext))


def install(monkeypatch: Any) -> type:
    """Route every KMS async client built from now on to `FakeKmsClient`; returns the class (its `instances`)."""
    monkeypatch.setattr(FakeKmsClient, "instances", [])
    monkeypatch.setattr("google.cloud.kms.KeyManagementServiceAsyncClient", FakeKmsClient)
    return FakeKmsClient
