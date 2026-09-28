from unittest.mock import MagicMock

import pytest
from quart import Quart, g, jsonify

from src.web.cabinet_auth import make_auth_required


def _app(verify):
    session_service = MagicMock()
    session_service.verify_access_token = verify
    auth_required = make_auth_required(session_service)
    app = Quart(__name__)

    @app.route("/who")
    @auth_required
    async def who():
        return jsonify({"user": g.user_id, "account": g.account_id, "role": g.role})

    return app


@pytest.mark.asyncio
async def test_bearer_token_sets_identity():
    app = _app(MagicMock(return_value={"sub": "u1", "account_id": "a1"}))
    resp = await app.test_client().get("/who", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    assert await resp.get_json() == {"user": "u1", "account": "a1", "role": "viewer"}


@pytest.mark.asyncio
async def test_cookie_token_is_accepted():
    verify = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    client = _app(verify).test_client()
    client.set_cookie("localhost", "access_token", "cookietok")
    resp = await client.get("/who")
    assert resp.status_code == 200
    verify.assert_called_once_with("cookietok")


@pytest.mark.asyncio
async def test_missing_and_invalid_tokens_are_401():
    app = _app(MagicMock(side_effect=ValueError("expired")))
    assert (await app.test_client().get("/who")).status_code == 401
    assert (await app.test_client().get("/who", headers={"Authorization": "Bearer bad"})).status_code == 401
