"""WebAuthnSignerVerifier — `SignerVerifierPort` over `webauthn` (py_webauthn, Duo Labs; CABINET_DEVICE_SESSIONS_RFC
§7.3, F8). The only module that imports the library.

The relying party is fixed at construction: `rp_id` (the host of `OAUTH_REDIRECT_URI`, F7) and `origin` (that URI's
scheme and host). Options per §7.3: a discoverable credential (`residentKey: required`), `userVerification: required`
on both ceremonies, `attestation: none`, and `user.id` = the user_id's UTF-8 bytes. The algorithms are the library's
defaults (EdDSA, ES256, RS256), offered and accepted alike.

The library checks the ceremony; this adapter maps it onto the port: options to a JSON dict (`options_to_json`), the
verified values to `PasskeyRegistration` / `PasskeyAssertion`, and every refusal, a malformed response included (the
library's `WebAuthnException`s, and a `ValueError` from a base64url or CBOR decode), to `PasskeyRejected`, logged
without the response.
"""
import json
from typing import Any, Dict, NoReturn, Sequence

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import decode_credential_public_key, parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..domain.passkey_ceremony import PasskeyAssertion, PasskeyRegistration, PasskeyRejected
from ..ports.signer_verifier import SignerVerifierPort
from ..utils.logger import logger

_REFUSED = (WebAuthnException, ValueError)


def _refuse(step: str, exc: Exception) -> NoReturn:
    logger.warning(f"⛔ Passkey {step} refused: {type(exc).__name__}: {exc}")
    raise PasskeyRejected(f"{step}: {exc}") from exc


class WebAuthnSignerVerifier(SignerVerifierPort):

    def __init__(self, *, rp_id: str, rp_name: str, origin: str) -> None:
        if not (rp_id and rp_name and origin):
            raise ValueError("a WebAuthn relying party needs an RP ID, a name and an origin")
        self._rp_id = rp_id
        self._rp_name = rp_name
        self._origin = origin

    async def registration_options(self, *, user_id: str, user_name: str, display_name: str, challenge: bytes,
                                   exclude_credential_ids: Sequence[bytes]) -> Dict[str, Any]:
        options = generate_registration_options(
            rp_id=self._rp_id, rp_name=self._rp_name, user_id=user_id.encode("utf-8"), user_name=user_name,
            user_display_name=display_name, challenge=challenge,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=cid) for cid in exclude_credential_ids])
        parsed: Dict[str, Any] = json.loads(options_to_json(options))
        return parsed

    async def verify_registration(self, response: Dict[str, Any], *, challenge: bytes) -> PasskeyRegistration:
        try:
            verified = verify_registration_response(
                credential=response, expected_challenge=challenge, expected_rp_id=self._rp_id,
                expected_origin=self._origin, require_user_verification=True)
            algorithm = int(decode_credential_public_key(verified.credential_public_key).alg)
        except _REFUSED as exc:
            _refuse("registration", exc)
        return PasskeyRegistration(
            credential_id=verified.credential_id, public_key=verified.credential_public_key, algorithm=algorithm,
            sign_count=verified.sign_count,
            backup_eligible=verified.credential_device_type == CredentialDeviceType.MULTI_DEVICE)

    async def authentication_options(self, *, challenge: bytes) -> Dict[str, Any]:
        options = generate_authentication_options(
            rp_id=self._rp_id, challenge=challenge, user_verification=UserVerificationRequirement.REQUIRED)
        parsed: Dict[str, Any] = json.loads(options_to_json(options))
        return parsed

    async def credential_id_of(self, response: Dict[str, Any]) -> bytes:
        try:
            raw_id: bytes = parse_authentication_credential_json(response).raw_id
        except _REFUSED as exc:
            _refuse("assertion", exc)
        return raw_id

    async def verify_authentication(self, response: Dict[str, Any], *, challenge: bytes, public_key: bytes,
                                    sign_count: int) -> PasskeyAssertion:
        try:
            credential = parse_authentication_credential_json(response)
            verified = verify_authentication_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self._rp_id,
                expected_origin=self._origin, credential_public_key=public_key,
                credential_current_sign_count=sign_count, require_user_verification=True)
        except _REFUSED as exc:
            _refuse("assertion", exc)
        return PasskeyAssertion(credential_id=verified.credential_id, sign_count=verified.new_sign_count,
                                user_handle=credential.response.user_handle)
