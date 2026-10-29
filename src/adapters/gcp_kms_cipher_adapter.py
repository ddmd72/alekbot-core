"""
GcpKmsCipherAdapter — CipherPort over Cloud KMS symmetric encrypt/decrypt (SECRETS_AT_REST_RFC §2.2).

Direct encrypt, no envelope: every stored secret is far below the 64 KiB symmetric plaintext limit. The CRC32C
fields are sent and checked both ways (Cloud KMS data-integrity guidelines); any mismatch, false `verified_*` flag
or SDK error raises after one log line, never a degraded value (I4). The key name comes from configuration
(`KMS_CRYPTO_KEY`), never a tracked file.

The async client is created on first use, inside the running event loop (plan D-P6: the indexer builds its app
synchronously, outside any loop). Creation has no await between the check and the assignment, so concurrent first
uses on one loop cannot build two clients.
"""

import base64
from typing import Any, Optional

import google_crc32c
from google.cloud import kms

from ..ports.cipher_port import CipherPort
from ..utils.logger import logger


def _crc32c(data: bytes) -> int:
    return int(google_crc32c.value(data))


class GcpKmsCipherAdapter(CipherPort):
    """CipherPort backed by one Cloud KMS `ENCRYPT_DECRYPT` key; `aad` is sent as additional authenticated data."""

    def __init__(self, key_name: str, client: Optional[Any] = None):
        self._key_name = key_name
        self._client = client

    def _kms(self) -> Any:
        if self._client is None:
            self._client = kms.KeyManagementServiceAsyncClient()
        return self._client

    async def encrypt(self, plaintext: str, *, aad: str) -> str:
        data = plaintext.encode("utf-8")
        aad_bytes = aad.encode("utf-8")
        try:
            response = await self._kms().encrypt(request={
                "name": self._key_name,
                "plaintext": data,
                "plaintext_crc32c": _crc32c(data),
                "additional_authenticated_data": aad_bytes,
                "additional_authenticated_data_crc32c": _crc32c(aad_bytes),
            })
        except Exception as e:
            logger.error(f"[Cipher] KMS encrypt failed: {type(e).__name__}: {e}")
            raise
        if not response.verified_plaintext_crc32c:
            raise self._integrity("encrypt", "plaintext CRC32C not verified by KMS")
        if not response.verified_additional_authenticated_data_crc32c:
            raise self._integrity("encrypt", "AAD CRC32C not verified by KMS")
        if response.ciphertext_crc32c != _crc32c(response.ciphertext):
            raise self._integrity("encrypt", "ciphertext CRC32C mismatch")
        return base64.b64encode(response.ciphertext).decode("ascii")

    async def decrypt(self, ciphertext: str, *, aad: str) -> str:
        try:
            data = base64.b64decode(ciphertext, validate=True)
        except ValueError as e:
            logger.error(f"[Cipher] stored ciphertext is not base64: {e}")
            raise
        aad_bytes = aad.encode("utf-8")
        try:
            response = await self._kms().decrypt(request={
                "name": self._key_name,
                "ciphertext": data,
                "ciphertext_crc32c": _crc32c(data),
                "additional_authenticated_data": aad_bytes,
                "additional_authenticated_data_crc32c": _crc32c(aad_bytes),
            })
        except Exception as e:
            logger.error(f"[Cipher] KMS decrypt failed: {type(e).__name__}: {e}")
            raise
        if response.plaintext_crc32c != _crc32c(response.plaintext):
            raise self._integrity("decrypt", "plaintext CRC32C mismatch")
        return bytes(response.plaintext).decode("utf-8")

    @staticmethod
    def _integrity(op: str, reason: str) -> RuntimeError:
        logger.error(f"[Cipher] KMS {op} integrity check failed: {reason}")
        return RuntimeError(f"KMS {op} integrity check failed: {reason}")
