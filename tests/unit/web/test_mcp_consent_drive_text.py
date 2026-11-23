"""The consent page picks its text by the bound scope: drive_read -> the drive text, else today's memory text."""

from unittest.mock import AsyncMock, Mock

from quart import Quart

from src.services.mcp_authorization_service import ConsentRequest
from src.web.mcp_consent_app import build_mcp_consent_blueprint
from tests.support.cabinet_session_kit import CabinetSessionKit
_KIT = CabinetSessionKit()


def _sign_in(client) -> None:
    """The consenting user's Cabinet session: a live device session's access cookie."""
    _KIT.set_cookies(client, _KIT.sign_in_now("user-1", "acc-1"), refresh=False)


_MARKUP = '<script>alert(1)</script>'
DRIVE = "https://mcp.example.test/drive-mcp"


async def _page(scopes, client_name="Example Client", resource="https://mcp.example.test/mcp") -> str:
    svc = Mock()
    svc.verify_consent_request = Mock(return_value=ConsentRequest(
        client_id="mcp-x", redirect_uri="https://claude.ai/api/mcp/auth_callback", code_challenge="c" * 43,
        code_challenge_method="S256", resource=resource, scopes=scopes, state="s"))
    svc.get_client = AsyncMock(return_value=Mock(client_name=client_name))
    app = Quart("test_consent_drive_text")
    app.register_blueprint(build_mcp_consent_blueprint(svc, _KIT.verifier))
    async with app.test_client() as client:
        _sign_in(client)
        resp = await client.get("/mcp/consent", query_string={"req": "JWT"})
        assert resp.status_code == 200
        return (await resp.get_data()).decode()


async def test_a_drive_grant_shows_the_drive_text():
    body = await _page(["drive_read"], resource=DRIVE)
    assert "Authorize drive connector" in body
    assert "<strong>Example Client</strong> is asking to read your drives through alekbot" in body
    assert "Connector access" in body
    assert "cannot change, move or delete anything" in body
    assert "<code>drive_read</code>" in body
    assert "biographical facts" not in body and "Authorize MCP connector" not in body


async def test_a_facts_grant_shows_todays_text():
    body = await _page(["user_context"])
    assert "Authorize MCP connector" in body
    assert "biographical facts and memories" in body
    assert "drives" not in body and "Connector access" not in body


async def test_the_drive_text_escapes_the_client_name():
    body = await _page(["drive_read"], client_name=_MARKUP, resource=DRIVE)
    assert "<script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
