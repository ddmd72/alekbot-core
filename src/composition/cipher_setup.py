"""Composition helper: the cipher and the digest key of the stored secrets, fail closed (SECRETS_AT_REST_RFC §2.6, I6).

Both are built by `build_application_graph`, the composition root, outside the MCP build: `main()` swallows any error of
that build and boots without the connector, which would turn "fail closed" into "MCP silently off" (plan D-P1). Unlike
`AuthConfig`'s placeholder checks, these run on every start, not only under `K_SERVICE`: local dev uses the real key
(§2.7), so an empty value is a misconfiguration everywhere.
"""
from typing import Optional

from ..adapters.gcp_kms_cipher_adapter import GcpKmsCipherAdapter
from ..ports.cipher_port import CipherPort

DIGEST_KEY_MIN_BYTES = 32


def build_cipher(key_name: Optional[str]) -> CipherPort:
    """The KMS cipher over `key_name` (`KMS_CRYPTO_KEY`); raises ValueError when it is empty. Calls no KMS API."""
    name = (key_name or "").strip()
    if not name:
        raise ValueError("KMS_CRYPTO_KEY must be set (the Cloud KMS key of stored secrets). Refusing to start.")
    return GcpKmsCipherAdapter(name)


def digest_key(raw: Optional[str]) -> bytes:
    """The bytes of `SECRET_DIGEST_KEY` (hex); raises ValueError when it is empty, not hex, or under 32 bytes."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("SECRET_DIGEST_KEY must be set (hex, at least 32 bytes). Refusing to start.")
    try:
        key = bytes.fromhex(text)
    except ValueError:
        raise ValueError("SECRET_DIGEST_KEY must be hex (at least 32 bytes). Refusing to start.") from None
    if len(key) < DIGEST_KEY_MIN_BYTES:
        raise ValueError(
            f"SECRET_DIGEST_KEY must be at least {DIGEST_KEY_MIN_BYTES} bytes, got {len(key)}. Refusing to start.")
    return key
