"""The provider's own redirect check on /authorize (P7, C1: `src/composition/mcp_sdk_oauth_provider.py:170-178`).

The SDK's /authorize handler validates `redirect_uri` against the client before it calls the provider, so through the
route this check is defence in depth: it is exercised here on the provider directly, over the real service and the
real Firestore MCP repository on the evaluating fake."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import AuthorizationParams
from pydantic import AnyUrl

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.composition.mcp_sdk_oauth_provider import MCPSdkOAuthProvider
from src.domain.mcp import MCPClient
from src.services.mcp_authorization_service import MCPAuthError, MCPAuthorizationService
from tests.support.firestore_eval import EvalDb
from tests.support.aes_gcm_cipher import AesGcmTestCipher

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


async def test_a_redirect_uri_the_client_never_registered_is_refused():
    db = EvalDb()
    service = MCPAuthorizationService(
        FirestoreMCPClientRepository(db, SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a",
                                                         mcp_refresh_tokens_collection="r"), cipher=AesGcmTestCipher(), digest_key=bytes(range(32))),
        jwt_secret="p7-mcp-jwt-secret-0123456789abcdef-xyz", mcp_resource_uri="https://mcp.example.test/mcp",
        consent_base_url="https://mcp.example.test/mcp/consent", allowed_redirect_hosts=("claude.ai",))
    await service.save_client(MCPClient(client_id="c1", client_secret="s", redirect_uris=[REDIRECT],
                                        created_at=datetime.now(timezone.utc)))
    provider = MCPSdkOAuthProvider(service)
    client = await provider.get_client("c1")
    params = AuthorizationParams(state="s", scopes=["user_context"], code_challenge="c" * 43,
                                 redirect_uri=AnyUrl("https://claude.ai/somewhere-else"),
                                 redirect_uri_provided_explicitly=True, resource=None)

    with pytest.raises(MCPAuthError, match="redirect_uri not allowed"):
        await provider.authorize(client, params)

    ok = await provider.authorize(client, params.model_copy(update={"redirect_uri": AnyUrl(REDIRECT)}))
    assert ok.startswith("https://mcp.example.test/mcp/consent?req=")   # the registered one is accepted
    assert db.collection("a").docs == {}                                 # no code is issued at /authorize
