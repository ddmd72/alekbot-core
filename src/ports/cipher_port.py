"""
CipherPort — encrypt and decrypt one persisted secret, bound to its record.

See docs/10_rfcs/SECRETS_AT_REST_RFC.md §2.2 and invariants I2-I4.
"""

from abc import ABC, abstractmethod


class CipherPort(ABC):
    """
    Port for application-layer encryption of a stored secret (a client secret, a token payload).

    Holds for every method:
    - Binding (I2): `aad` is required and keyword-only, of the form `<store>/<record key>`. A ciphertext decrypts
      only under the `aad` it was encrypted with, so it cannot move to another record or store.
    - Format: the ciphertext is base64 text, stored as is.
    - Errors (I4): a wrong key, a tampered ciphertext, a mismatched `aad` or a backend failure raises. No method
      returns an empty value, the input, or a default instead.
    - Neutral: the port names no provider, store or secret.
    """

    @abstractmethod
    async def encrypt(self, plaintext: str, *, aad: str) -> str:
        """Return the base64 ciphertext of `plaintext`, bound to `aad`."""

    @abstractmethod
    async def decrypt(self, ciphertext: str, *, aad: str) -> str:
        """Return the plaintext of `ciphertext`; raises unless it was encrypted under this key with this `aad`."""
