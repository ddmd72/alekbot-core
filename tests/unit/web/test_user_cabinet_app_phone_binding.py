"""
Unit tests for phone binding with Twilio Verify OTP.

RFC: docs/10_rfcs/VOICE_COMPANION_RFC.md §4.6 — `link_telegram` never verified the
binder owns the platform ID; inheriting that trust model for a phone number would
permit squatting. These two routes add a one-time OTP step (Twilio Verify) before
`add_platform_id(..., "phone", ...)` is allowed to run.

Phone fixtures use short, non-realistic placeholders (e.g. "+346001") per the
Slice 1 plan ruling — the repo's pre-commit PII hook has no tests/ allowlist for
realistic 9-digit phone-shaped literals.
"""
from unittest.mock import MagicMock

from quart import Quart

from src.web.user_cabinet_app import create_user_cabinet_blueprint

_USER_ID = "user-1"
_ACCOUNT_ID = "account-1"


def _app(*, twilio_verify_client, user_repo=None):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(
        return_value={"sub": _USER_ID, "account_id": _ACCOUNT_ID, "role": "owner"}
    )
    bp = create_user_cabinet_blueprint(
        invite_service=MagicMock(),
        session_service=session_service,
        user_repo=user_repo or MagicMock(),
        fact_repo=MagicMock(),
        embedding_service=MagicMock(),
        twilio_verify_client=twilio_verify_client,
    )
    app = Quart("test_app")
    app.register_blueprint(bp)
    return app


class TestRequestPhoneOtp:

    async def test_request_phone_otp_starts_twilio_verification(self):
        twilio_verify_client = MagicMock()
        twilio_verify_client.verifications.create.return_value = MagicMock(status="pending")
        app = _app(twilio_verify_client=twilio_verify_client)

        async with app.test_client() as client:
            resp = await client.post(
                "/api/user/request-phone-otp",
                headers={"Authorization": "Bearer token"},
                json={"phone_number": "+346001"},
            )

        assert resp.status_code == 200
        twilio_verify_client.verifications.create.assert_called_once_with(
            to="+346001", channel="sms"
        )

    async def test_request_phone_otp_rejects_non_e164(self):
        twilio_verify_client = MagicMock()
        app = _app(twilio_verify_client=twilio_verify_client)

        async with app.test_client() as client:
            resp = await client.post(
                "/api/user/request-phone-otp",
                headers={"Authorization": "Bearer token"},
                json={"phone_number": "346001"},
            )

        assert resp.status_code == 400
        twilio_verify_client.verifications.create.assert_not_called()

    async def test_request_phone_otp_501_when_not_configured(self):
        app = _app(twilio_verify_client=None)

        async with app.test_client() as client:
            resp = await client.post(
                "/api/user/request-phone-otp",
                headers={"Authorization": "Bearer token"},
                json={"phone_number": "+346001"},
            )

        assert resp.status_code == 501


class TestVerifyPhoneOtp:

    async def test_verify_phone_otp_binds_on_approved_code(self):
        twilio_verify_client = MagicMock()
        twilio_verify_client.verification_checks.create.return_value = MagicMock(status="approved")
        user_repo = MagicMock()
        from unittest.mock import AsyncMock
        user_repo.add_platform_id = AsyncMock()
        app = _app(twilio_verify_client=twilio_verify_client, user_repo=user_repo)

        async with app.test_client() as client:
            resp = await client.post(
                "/api/user/verify-phone-otp",
                headers={"Authorization": "Bearer token"},
                json={"phone_number": "+346001", "code": "123456"},
            )

        assert resp.status_code == 200
        twilio_verify_client.verification_checks.create.assert_called_once_with(
            to="+346001", code="123456"
        )
        user_repo.add_platform_id.assert_awaited_once_with(_USER_ID, "phone", "+346001")

    async def test_verify_phone_otp_rejects_wrong_code(self):
        twilio_verify_client = MagicMock()
        twilio_verify_client.verification_checks.create.return_value = MagicMock(status="denied")
        user_repo = MagicMock()
        from unittest.mock import AsyncMock
        user_repo.add_platform_id = AsyncMock()
        app = _app(twilio_verify_client=twilio_verify_client, user_repo=user_repo)

        async with app.test_client() as client:
            resp = await client.post(
                "/api/user/verify-phone-otp",
                headers={"Authorization": "Bearer token"},
                json={"phone_number": "+346001", "code": "000000"},
            )

        assert resp.status_code == 400
        user_repo.add_platform_id.assert_not_called()

    async def test_verify_phone_otp_409_when_already_linked(self):
        twilio_verify_client = MagicMock()
        twilio_verify_client.verification_checks.create.return_value = MagicMock(status="approved")
        user_repo = MagicMock()
        from unittest.mock import AsyncMock
        user_repo.add_platform_id = AsyncMock(
            side_effect=ValueError("This phone number is already linked to another account")
        )
        app = _app(twilio_verify_client=twilio_verify_client, user_repo=user_repo)

        async with app.test_client() as client:
            resp = await client.post(
                "/api/user/verify-phone-otp",
                headers={"Authorization": "Bearer token"},
                json={"phone_number": "+346001", "code": "123456"},
            )

        assert resp.status_code == 409

    async def test_verify_phone_otp_501_when_not_configured(self):
        app = _app(twilio_verify_client=None)

        async with app.test_client() as client:
            resp = await client.post(
                "/api/user/verify-phone-otp",
                headers={"Authorization": "Bearer token"},
                json={"phone_number": "+346001", "code": "123456"},
            )

        assert resp.status_code == 501
