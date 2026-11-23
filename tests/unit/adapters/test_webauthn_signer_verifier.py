"""WebAuthnSignerVerifier at the library boundary (CABINET_DEVICE_SESSIONS_RFC §7.3; `docs/how_to/ADAPTER_WIRE_TESTING.md`):
the real `webauthn` library, never mocked, fed real ceremonies.

- The options as the browser receives them (§7.3: discoverable credential, user verification required, no
  attestation, `user.id` = the user_id bytes, the user's passkeys excluded; no credential named at sign-in).
- Real ceremony vectors published by the library's own test suite (py_webauthn 3.0.1, `tests/test_verify_registration_
  response.py::test_verifies_none_attestation_response` and `tests/test_verify_authentication_response.py`, relying
  party `localhost` at `http://localhost:5000`): a `none` registration and an RSA assertion with user verification
  pass; an EC2 assertion without user verification is refused.
- Generated ceremonies for `cabinet.example.com` (`tests/support/webauthn_authenticator.py`: an ES256 key, authenticator
  data and clientDataJSON built by hand): a round trip, the BE flag, and every refusal mapped to `PasskeyRejected`.
"""
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from src.adapters.webauthn_signer_verifier import WebAuthnSignerVerifier
from src.domain.passkey_ceremony import PasskeyAssertion, PasskeyRegistration, PasskeyRejected
from tests.support.webauthn_authenticator import SoftAuthenticator, b64u, unb64u

RP, ORIGIN = "cabinet.example.com", "https://cabinet.example.com"
CHALLENGE, OTHER_CHALLENGE = b"c" * 32, b"o" * 32


def verifier(rp_id=RP, origin=ORIGIN) -> WebAuthnSignerVerifier:
    return WebAuthnSignerVerifier(rp_id=rp_id, rp_name="Alek", origin=origin)


async def registration_options(v, challenge=CHALLENGE, exclude=()):
    return await v.registration_options(user_id="user-alex", user_name="alex@example.com", display_name="Alex",
                                        challenge=challenge, exclude_credential_ids=list(exclude))


async def registered(authenticator=None, v=None):
    """A verified registration on `cabinet.example.com`: (verifier, authenticator, registration)."""
    v = v or verifier()
    authenticator = authenticator or SoftAuthenticator(RP, ORIGIN)
    response = authenticator.register(await registration_options(v))
    return v, authenticator, await v.verify_registration(response, challenge=CHALLENGE)


# ---------------------------------------------------------------------------------------------- the options

class TestOptions:
    async def test_registration_options_are_the_rfc_ones(self):
        options = await registration_options(verifier(), exclude=[b"\x01\x02", b"\x03"])
        assert options == {
            "rp": {"name": "Alek", "id": RP},
            "user": {"id": b64u(b"user-alex"), "name": "alex@example.com", "displayName": "Alex"},
            "challenge": b64u(CHALLENGE),
            "pubKeyCredParams": [{"type": "public-key", "alg": -8}, {"type": "public-key", "alg": -7},
                                 {"type": "public-key", "alg": -257}],
            "timeout": 60000,
            "excludeCredentials": [{"id": b64u(b"\x01\x02"), "type": "public-key"},
                                   {"id": b64u(b"\x03"), "type": "public-key"}],
            "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True,
                                       "userVerification": "required"},
            "attestation": "none",
        }

    async def test_authentication_options_name_no_credential_and_require_verification(self):
        options = await verifier().authentication_options(challenge=CHALLENGE)
        assert options == {"challenge": b64u(CHALLENGE), "timeout": 60000, "rpId": RP, "allowCredentials": [],
                           "userVerification": "required"}

    @pytest.mark.parametrize("rp_id, rp_name, origin", [("", "Alek", ORIGIN), (RP, "", ORIGIN), (RP, "Alek", "")])
    def test_a_relying_party_needs_all_three_values(self, rp_id, rp_name, origin):
        with pytest.raises(ValueError, match="relying party"):
            WebAuthnSignerVerifier(rp_id=rp_id, rp_name=rp_name, origin=origin)


# ---------------------------------------------------------------------------------------------- the library's vectors

LOCALHOST, LOCAL_ORIGIN = "localhost", "http://localhost:5000"

NONE_REGISTRATION = {
    "id": "9y1xA8Tmg1FEmT-c7_fvWZ_uoTuoih3OvR45_oAK-cwHWhAbXrl2q62iLVTjiyEZ7O7n-CROOY494k7Q3xrs_w",
    "rawId": "9y1xA8Tmg1FEmT-c7_fvWZ_uoTuoih3OvR45_oAK-cwHWhAbXrl2q62iLVTjiyEZ7O7n-CROOY494k7Q3xrs_w",
    "response": {
        "attestationObject": "o2NmbXRkbm9uZWdhdHRTdG10oGhhdXRoRGF0YVjESZYN5YgOjGh0NBcPZHZgW4_krrmihjLHmVzzuoMdl2NFAAAAFw"
                             "AAAAAAAAAAAAAAAAAAAAAAQPctcQPE5oNRRJk_nO_371mf7qE7qIodzr0eOf6ACvnMB1oQG165dqutoi1U44shGezu"
                             "5_gkTjmOPeJO0N8a7P-lAQIDJiABIVggSFbUJF-42Ug3pdM8rDRFu_N5oiVEysPDB6n66r_7dZAiWCDUVnB39FlGyp"
                             "L-qAoIO9xWHtJygo2jfDmHl-_eKFRLDA",
        "clientDataJSON": "eyJ0eXBlIjoid2ViYXV0aG4uY3JlYXRlIiwiY2hhbGxlbmdlIjoiVHdON240V1R5R0tMYzRaWS1xR3NGcUtuSE00bmd"
                          "scXN5VjBJQ0psTjJUTzlYaVJ5RnRya2FEd1V2c3FsLWdrTEpYUDZmbkYxTWxyWjUzTW00UjdDdnciLCJvcmlnaW4iOiJ"
                          "odHRwOi8vbG9jYWxob3N0OjUwMDAiLCJjcm9zc09yaWdpbiI6ZmFsc2V9",
    },
    "type": "public-key", "clientExtensionResults": {}, "transports": ["nfc", "usb"],
}
NONE_CHALLENGE = "TwN7n4WTyGKLc4ZY-qGsFqKnHM4nglqsyV0ICJlN2TO9XiRyFtrkaDwUvsql-gkLJXP6fnF1MlrZ53Mm4R7Cvw"
NONE_PUBLIC_KEY = "pQECAyYgASFYIEhW1CRfuNlIN6XTPKw0RbvzeaIlRMrDwwep-uq_-3WQIlgg1FZwd_RZRsqS_qgKCDvcVh7ScoKNo3w5h5fv3ihUSww"

RSA_ASSERTION = {
    "id": "ZoIKP1JQvKdrYj1bTUPJ2eTUsbLeFkv-X5xJQNr4k6s",
    "rawId": "ZoIKP1JQvKdrYj1bTUPJ2eTUsbLeFkv-X5xJQNr4k6s",
    "response": {
        "authenticatorData": "SZYN5YgOjGh0NBcPZHZgW4_krrmihjLHmVzzuoMdl2MFAAAAAQ",
        "clientDataJSON": "eyJ0eXBlIjoid2ViYXV0aG4uZ2V0IiwiY2hhbGxlbmdlIjoiaVBtQWkxUHAxWEw2b0FncTNQV1p0WlBuWmExekZVRG9"
                          "HYmFRMF9LdlZHMWxGMnMzUnRfM280dVN6Y2N5MHRtY1RJcFRUVDRCVTFULUk0bWFhdm5kalEiLCJvcmlnaW4iOiJodHRw"
                          "Oi8vbG9jYWxob3N0OjUwMDAiLCJjcm9zc09yaWdpbiI6ZmFsc2V9",
        "signature": "iOHKX3erU5_OYP_r_9HLZ-CexCE4bQRrxM8WmuoKTDdhAnZSeTP0sjECjvjfeS8MJzN1ArmvV0H0C3yy_FdRFfcpUPZzdZ7bBc"
                     "mPh1XPdxRwY747OrIzcTLTFQUPdn1U-izCZtP_78VGw9pCpdMsv4CUzZdJbEcRtQuRS03qUjqDaovoJhOqEBmxJn9Wu8tBi_Qx7A"
                     "33RbYjlfyLm_EDqimzDZhyietyop6XUcpKarKqVH0M6mMrM5zTjp8xf3W7odFCadXEJg-ERZqFM0-9Uup6kJNLbr6C5J4NDYmSm3"
                     "HCSA6lp2iEiMPKU8Ii7QZ61kybXLxsX4w4Dm3fOLjmDw",
        "userHandle": "T1RWa1l6VXdPRFV0WW1NNVlTMDBOVEkxTFRnd056Z3RabVZpWVdZNFpEVm1ZMk5p",
    },
    "type": "public-key", "clientExtensionResults": {},
}
RSA_CHALLENGE = "iPmAi1Pp1XL6oAgq3PWZtZPnZa1zFUDoGbaQ0_KvVG1lF2s3Rt_3o4uSzccy0tmcTIpTTT4BU1T-I4maavndjQ"
RSA_PUBLIC_KEY = ("pAEDAzkBACBZAQDfV20epzvQP-HtcdDpX-cGzdOxy73WQEvsU7Dnr9UWJophEfpngouvgnRLXaEUn_d8HGkp_HIx8rrpkx4BVs6X_B"
                  "6ZjhLlezjIdJbLbVeb92BaEsmNn1HW2N9Xj2QM8cH-yx28_vCjf82ahQ9gyAr552Bn96G22n8jqFRQKdVpO-f-bvpvaP3IQ9F5LCX7"
                  "CUaxptgbog1SFO6FI6ob5SlVVB00lVXsaYg8cIDZxCkkENkGiFPgwEaZ7995SCbiyCpUJbMqToLMgojPkAhWeyktu7TlK6UBWdJMHc"
                  "3FPAIs0lH_2_2hKS-mGI1uZAFVAfW1X-mzKL0czUm2P1UlUox7IUMBAAE")

EC2_ASSERTION_NO_UV = {
    "id": "EDx9FfAbp4obx6oll2oC4-CZuDidRVV4gZhxC529ytlnqHyqCStDUwfNdm1SNHAe3X5KvueWQdAX3x9R1a2b9Q",
    "rawId": "EDx9FfAbp4obx6oll2oC4-CZuDidRVV4gZhxC529ytlnqHyqCStDUwfNdm1SNHAe3X5KvueWQdAX3x9R1a2b9Q",
    "response": {
        "authenticatorData": "SZYN5YgOjGh0NBcPZHZgW4_krrmihjLHmVzzuoMdl2MBAAAATg",
        "clientDataJSON": "eyJjaGFsbGVuZ2UiOiJ4aTMwR1BHQUZZUnhWRHBZMXNNMTBEYUx6VlFHNjZudi1fN1JVYXpIMHZJMll2RzhMWWdERW5"
                          "2TjVmWlpOVnV2RUR1TWk5dGUzVkxxYjQyTjBma0xHQSIsImNsaWVudEV4dGVuc2lvbnMiOnt9LCJoYXNoQWxnb3JpdGh"
                          "tIjoiU0hBLTI1NiIsIm9yaWdpbiI6Imh0dHA6Ly9sb2NhbGhvc3Q6NTAwMCIsInR5cGUiOiJ3ZWJhdXRobi5nZXQifQ",
        "signature": "MEUCIGisVZOBapCWbnJJvjelIzwpixxIwkjCCb5aCHafQu68AiEA88v-2pJNNApPFwAKFiNuf82-2hBxYW5kGwVweeoxCwo",
    },
    "type": "public-key", "clientExtensionResults": {},
}
EC2_CHALLENGE = "xi30GPGAFYRxVDpY1sM10DaLzVQG66nv-_7RUazH0vI2YvG8LYgDEnvN5fZZNVuvEDuMi9te3VLqb42N0fkLGA"
EC2_PUBLIC_KEY = "pQECAyYgASFYIIeDTe-gN8A-zQclHoRnGFWN8ehM1b7yAsa8I8KIvmplIlgg4nFGT5px8o6gpPZZhO01wdy9crDSA_Ngtkx0vGpvPHI"


class TestLibraryVectors:
    async def test_a_real_none_registration_verifies(self):
        reg = await verifier(LOCALHOST, LOCAL_ORIGIN).verify_registration(NONE_REGISTRATION,
                                                                          challenge=unb64u(NONE_CHALLENGE))
        assert reg == PasskeyRegistration(credential_id=unb64u(NONE_REGISTRATION["rawId"]),
                                          public_key=unb64u(NONE_PUBLIC_KEY), algorithm=-7, sign_count=23,
                                          backup_eligible=False)

    async def test_a_real_verified_rsa_assertion_verifies_and_carries_its_user_handle(self):
        v = verifier(LOCALHOST, LOCAL_ORIGIN)
        assert await v.credential_id_of(RSA_ASSERTION) == unb64u(RSA_ASSERTION["rawId"])
        assertion = await v.verify_authentication(RSA_ASSERTION, challenge=unb64u(RSA_CHALLENGE),
                                                  public_key=unb64u(RSA_PUBLIC_KEY), sign_count=0)
        assert assertion == PasskeyAssertion(credential_id=unb64u(RSA_ASSERTION["rawId"]), sign_count=1,
                                             user_handle=unb64u(RSA_ASSERTION["response"]["userHandle"]))

    async def test_a_real_assertion_without_user_verification_is_refused(self):
        with pytest.raises(PasskeyRejected, match="assertion: User verification is required"):
            await verifier(LOCALHOST, LOCAL_ORIGIN).verify_authentication(
                EC2_ASSERTION_NO_UV, challenge=unb64u(EC2_CHALLENGE), public_key=unb64u(EC2_PUBLIC_KEY),
                sign_count=77)

    async def test_the_real_registration_on_another_relying_party_is_refused(self):
        with pytest.raises(PasskeyRejected, match="origin"):
            await verifier().verify_registration(NONE_REGISTRATION, challenge=unb64u(NONE_CHALLENGE))


# ---------------------------------------------------------------------------------------------- generated ceremonies

class TestGeneratedCeremonies:
    async def test_a_registration_and_an_assertion_round_trip(self):
        v, authenticator, reg = await registered()
        [cred] = authenticator.credentials
        assert reg == PasskeyRegistration(credential_id=cred.credential_id, public_key=cred.cose_public_key(),
                                          algorithm=-7, sign_count=0, backup_eligible=False)
        response = authenticator.sign_in(await v.authentication_options(challenge=OTHER_CHALLENGE))
        assert await v.credential_id_of(response) == cred.credential_id
        assertion = await v.verify_authentication(response, challenge=OTHER_CHALLENGE, public_key=reg.public_key,
                                                  sign_count=0)
        assert assertion == PasskeyAssertion(credential_id=cred.credential_id, sign_count=1, user_handle=b"user-alex")

    async def test_a_synced_passkey_is_backup_eligible_and_may_keep_its_counter_at_zero(self):
        v, authenticator, reg = await registered(SoftAuthenticator(RP, ORIGIN, backup_eligible=True, counter_step=0))
        assert reg.backup_eligible is True
        response = authenticator.sign_in(await v.authentication_options(challenge=OTHER_CHALLENGE))
        assertion = await v.verify_authentication(response, challenge=OTHER_CHALLENGE, public_key=reg.public_key,
                                                  sign_count=0)
        assert assertion.sign_count == 0

    async def test_an_assertion_without_a_user_handle_carries_none(self):
        v, authenticator, reg = await registered()
        response = authenticator.sign_in(await v.authentication_options(challenge=OTHER_CHALLENGE), user_handle=None)
        assertion = await v.verify_authentication(response, challenge=OTHER_CHALLENGE, public_key=reg.public_key,
                                                  sign_count=0)
        assert assertion.user_handle is None

    @pytest.mark.parametrize("change, match", [
        (dict(user_verified=False), "User verification is required"),
        (dict(origin="https://evil.example.com"), "origin"),
        (dict(rp_id="other.example.com"), "RP ID"),
    ], ids=["no-user-verification", "other-origin", "other-rp"])
    async def test_a_registration_that_does_not_hold_is_refused(self, change, match):
        v = verifier()
        response = SoftAuthenticator(RP, ORIGIN).register(await registration_options(v), **change)
        with pytest.raises(PasskeyRejected, match=match):
            await v.verify_registration(response, challenge=CHALLENGE)

    async def test_a_registration_for_another_challenge_is_refused(self):
        v = verifier()
        response = SoftAuthenticator(RP, ORIGIN).register(await registration_options(v))
        with pytest.raises(PasskeyRejected, match="challenge"):
            await v.verify_registration(response, challenge=OTHER_CHALLENGE)

    @pytest.mark.parametrize("change, stored_count, match", [
        (dict(user_verified=False), 0, "User verification is required"),
        (dict(origin="https://evil.example.com"), 0, "origin"),
        (dict(counter=5), 5, "sign count"),
        (dict(counter=4), 5, "sign count"),
        (dict(counter=0), 5, "sign count"),
        (dict(signer=ec.generate_private_key(ec.SECP256R1())), 0, "signature"),
    ], ids=["no-user-verification", "other-origin", "same-counter", "lower-counter", "zero-after-nonzero",
            "other-key"])
    async def test_an_assertion_that_does_not_hold_is_refused(self, change, stored_count, match):
        v, authenticator, reg = await registered()
        response = authenticator.sign_in(await v.authentication_options(challenge=OTHER_CHALLENGE), **change)
        with pytest.raises(PasskeyRejected, match=match):
            await v.verify_authentication(response, challenge=OTHER_CHALLENGE, public_key=reg.public_key,
                                          sign_count=stored_count)

    async def test_an_assertion_for_another_challenge_is_refused(self):
        v, authenticator, reg = await registered()
        response = authenticator.sign_in(await v.authentication_options(challenge=OTHER_CHALLENGE))
        with pytest.raises(PasskeyRejected, match="challenge"):
            await v.verify_authentication(response, challenge=CHALLENGE, public_key=reg.public_key, sign_count=0)

    @pytest.mark.parametrize("response", [
        {}, {"id": "a", "rawId": "a", "type": "public-key", "response": "not-an-object"},
        {"id": "a", "rawId": "a", "type": "public-key",
         "response": {"clientDataJSON": "a", "authenticatorData": "a", "signature": "a", "attestationObject": "a"}},
    ], ids=["empty", "response-not-an-object", "not-base64url"])
    async def test_a_malformed_response_is_refused_at_every_step(self, response):
        v = verifier()
        for step in (v.credential_id_of(response),
                     v.verify_authentication(response, challenge=CHALLENGE, public_key=b"", sign_count=0),
                     v.verify_registration(response, challenge=CHALLENGE)):
            with pytest.raises(PasskeyRejected):
                await step
