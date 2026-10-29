"""The test cipher (SECRETS_AT_REST_RFC §2.7): real authenticated encryption with the production AAD semantics.

`AesGcmTestCipher` implements `CipherPort` over `cryptography`'s AESGCM with a random 96-bit nonce per call and the
`aad` bound as associated data. A wrong `aad`, a flipped byte or another key raises `cryptography.exceptions.InvalidTag`.
It is never an identity function, so a store test over it proves that no plaintext reaches a stored field.
"""
import base64
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.ports.cipher_port import CipherPort

_NONCE_BYTES = 12


class AesGcmTestCipher(CipherPort):
    """AES-256-GCM; ciphertext = base64(nonce ‖ AESGCM output). `key` defaults to 32 random bytes."""

    def __init__(self, key: Optional[bytes] = None):
        self._aead = AESGCM(key if key is not None else os.urandom(32))
        self.encrypts = 0
        self.decrypts = 0

    async def encrypt(self, plaintext: str, *, aad: str) -> str:
        self.encrypts += 1
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
        return base64.b64encode(nonce + sealed).decode("ascii")

    async def decrypt(self, ciphertext: str, *, aad: str) -> str:
        self.decrypts += 1
        raw = base64.b64decode(ciphertext, validate=True)
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return self._aead.decrypt(nonce, sealed, aad.encode("utf-8")).decode("utf-8")
