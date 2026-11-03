"""Every route of the drive connector app except the public protected-resource metadata refuses an anonymous caller."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx

from src.composition.drive_mcp_setup import build_drive_mcp_app
from src.composition.mcp_setup import build_mcp_components
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import allowing_repos

AUTH_CONFIG = SimpleNamespace(
    mcp_resource_uri="https://mcp.example.test/mcp", oauth_session_secret="route-jwt-secret-0123456789abcdef-xyz",
    mcp_access_token_ttl=3600, mcp_refresh_token_ttl=2592000, mcp_auth_code_ttl=600, mcp_consent_request_ttl=600)


def _drive_asgi():
    users, whitelist = allowing_repos("u1")
    components = build_mcp_components(
        db_client=EvalDb(), env_config=SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a",
                                                       mcp_refresh_tokens_collection="r"),
        auth_config=AUTH_CONFIG, search_enrichment_service=MagicMock(side_effect=AssertionError("reached anonymously")),
        cipher=AesGcmTestCipher(), digest_key=bytes(range(32)), user_repo=users, whitelist_repo=whitelist)
    return build_drive_mcp_app(authorization_service=components.authorization_service,
                               auth_config=AUTH_CONFIG).streamable_http_app()


def _routes(asgi):
    out = []
    for route in asgi.routes:
        methods = route.methods or {"GET", "POST", "DELETE"}    # the endpoint is a raw ASGI mount: all three
        out += [(m, route.path) for m in sorted(methods - {"HEAD", "OPTIONS"})]
    return out


def test_the_drive_app_serves_the_endpoint_and_only_public_metadata_besides():
    paths = {path for _, path in _routes(_drive_asgi())}
    assert "/drive-mcp" in paths
    assert {p for p in paths if p != "/drive-mcp"} == {"/.well-known/oauth-protected-resource/drive-mcp"}


async def test_every_drive_route_but_the_metadata_refuses_an_anonymous_caller():
    asgi = _drive_asgi()
    probes = [(m, p) for m, p in _routes(asgi) if not p.startswith("/.well-known/")]
    assert probes
    failures = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi, raise_app_exceptions=False),
                                 base_url="https://mcp.example.test") as http:
        for method, path in probes:
            resp = await http.request(method, path)
            if resp.status_code != 401:
                failures.append(f"{method} {path}: answered {resp.status_code}")
    assert failures == []
