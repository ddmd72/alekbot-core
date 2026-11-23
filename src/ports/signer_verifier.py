"""SignerVerifierPort — the boundary to the code that checks a passkey ceremony (CABINET_DEVICE_SESSIONS_RFC §7.3, §12).

Two ceremonies, each in two steps. The caller makes the challenge (random bytes it stores and later hands back) and
keeps the stored key and counter; the verifier builds the options the browser needs and checks what the browser
returns against them. It holds no state: everything it needs is an argument.

- Registration: `registration_options` for `navigator.credentials.create`, then `verify_registration` of the returned
  credential → `PasskeyRegistration`.
- Sign-in with a discoverable credential (no user named first): `authentication_options` for
  `navigator.credentials.get`; `credential_id_of` reads which credential answered, so the caller can load its stored
  key; `verify_authentication` checks the assertion against that key and counter → `PasskeyAssertion`.

Every response is the browser's credential as a JSON dict, and every key, id and challenge is bytes: no library type
crosses the port, so a second implementation (the native approver of §12, ECDSA over the challenge) fits it.
Every refusal of a response, a malformed one included, raises `PasskeyRejected`.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Sequence

from src.domain.passkey_ceremony import PasskeyAssertion, PasskeyRegistration


class SignerVerifierPort(ABC):

    @abstractmethod
    async def registration_options(self, *, user_id: str, user_name: str, display_name: str, challenge: bytes,
                                   exclude_credential_ids: Sequence[bytes]) -> Dict[str, Any]:
        """The creation options for `user_id` (its UTF-8 bytes are the credential's user handle), named `user_name`
        and shown as `display_name`, over `challenge`, refusing the credentials in `exclude_credential_ids` (the
        user's registered ones). A discoverable credential and user verification are required; no attestation."""

    @abstractmethod
    async def verify_registration(self, response: Dict[str, Any], *, challenge: bytes) -> PasskeyRegistration:
        """Verify the credential `navigator.credentials.create` returned for `challenge`: the ceremony type, the
        challenge, the origin and RP, user presence and verification, a supported key algorithm."""

    @abstractmethod
    async def authentication_options(self, *, challenge: bytes) -> Dict[str, Any]:
        """The request options over `challenge`, with no credential named (any passkey of this RP may answer) and
        user verification required."""

    @abstractmethod
    async def credential_id_of(self, response: Dict[str, Any]) -> bytes:
        """The raw id of the credential that produced an assertion response; nothing is verified yet."""

    @abstractmethod
    async def verify_authentication(self, response: Dict[str, Any], *, challenge: bytes, public_key: bytes,
                                    sign_count: int) -> PasskeyAssertion:
        """Verify the assertion `navigator.credentials.get` returned for `challenge` against the stored `public_key`
        and counter: the ceremony type, the challenge, the origin and RP, user presence and verification, a counter
        that advanced (`sign_count_advances`), and the signature."""
