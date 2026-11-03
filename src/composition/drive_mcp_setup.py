"""
Composition helper: the drive connector's FastMCP, a resource server only.

The facts instance (`mcp_setup.build_mcp_components`) stays the authorization server. This second instance serves
`/drive-mcp` with scope `drive_read` and verifies bearer tokens against the drive resource URI through the shared
`MCPAuthorizationService` (DRIVE_MCP_CONNECTOR_RFC §3.2). The SDK forbids an auth provider and a token verifier on
one instance, hence two. Named `drive_mcp_setup`, not `mcp_drive_setup`: the drive-notes isolation test treats
`mcp_*.py` as memory-side.
"""

from typing import Optional
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl

from ..config.auth import AuthConfig
from ..domain.mcp_resource import DRIVE_PATH, drive_resource_uri
from ..services.mcp_authorization_service import MCPAuthorizationService
from .mcp_sdk_oauth_provider import AlekAccessToken
from .mcp_setup import DRIVE_SCOPE, build_transport_security


class DriveTokenVerifier:
    """Satisfies mcp.server.auth.provider.TokenVerifier for the drive resource."""

    def __init__(self, service: MCPAuthorizationService, resource_uri: str) -> None:
        self._service = service
        self._resource_uri = resource_uri

    async def verify_token(self, token: str) -> Optional[AlekAccessToken]:
        verified = self._service.verify_access_token(token, resource=self._resource_uri)
        if verified is None:
            return None
        return AlekAccessToken(
            token=verified.token,
            client_id=verified.client_id,
            scopes=list(verified.scopes),
            expires_at=verified.expires_at,
            resource=verified.resource,
            user_id=verified.user_id,
            account_id=verified.account_id,
        )


def build_drive_mcp_app(
    *, authorization_service: MCPAuthorizationService, auth_config: AuthConfig
) -> FastMCP:
    drive_uri = drive_resource_uri(auth_config.mcp_resource_uri)
    parsed = urlparse(auth_config.mcp_resource_uri)
    issuer_root = f"{parsed.scheme}://{parsed.netloc}"

    app = FastMCP(
        name="alekbot-drive",
        instructions="alekbot drive connector: read-only access to the folders you share with it.",
        token_verifier=DriveTokenVerifier(authorization_service, drive_uri),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer_root),
            resource_server_url=AnyHttpUrl(drive_uri),
            required_scopes=[DRIVE_SCOPE],
        ),
        streamable_http_path=DRIVE_PATH,
        transport_security=build_transport_security(auth_config.mcp_resource_uri),
        stateless_http=True,  # Cloud Run may route subsequent requests to different instances
    )

    # Slice-1 placeholder so the connector lists one tool and the auth seam can be exercised end to end. It
    # carries no token-backed wording; slice 2 (the D4 token rule) deletes it.
    @app.tool(
        name="drive_connector_status",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def drive_connector_status() -> str:
        return "Connected. No drive tools yet."

    return app
