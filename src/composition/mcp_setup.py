"""
Composition helper: wire the remote MCP server (FastMCP + OAuth
provider) into existing infrastructure.

This module is the single place that knows about all layers:
- AuthConfig (config)
- FirestoreMCPClientRepository (adapter)
- MCPAuthorizationService (service)
- MCPSdkOAuthProvider (composition)
- FastMCP (SDK)
- FactsMemoryAgent (agent, via UserAgentFactory.get_memory_search) — the actual tool implementation

It exposes one function, `build_mcp_components`, which returns a
FastMCP instance (ready to be mounted via `streamable_http_app()`) and
the authorization service (which main.py passes to the consent
blueprint factory — the consent blueprint cannot be imported here
because composition/ must not depend on web/).
"""

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from ..adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..agents.memory_search_agent import FactsMemoryAgent
from ..domain.mcp_resource import MCPResource, drive_resource_uri
from ..ports.cipher_port import CipherPort
from ..ports.user_repository import UserRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..services.inactive_user_check import InactiveUserCheck
from ..services.mcp_authorization_service import MCPAuthorizationService
from ..utils.logger import logger
from .facts_tool_setup import FACTS_TOOL, register_facts_tool  # noqa: F401  (FACTS_TOOL re-exported)
from .mcp_sdk_oauth_provider import MCPSdkOAuthProvider


# OAuth scope advertised by the MCP server to claude.ai. Deliberately
# namespaced to avoid collision with the Firestore `user_context`
# collection name (which arch test REQ-ARCH-21 guards against in core
# layers). Composition is free of that restriction.
MCP_DEFAULT_SCOPE = "user_context"
# The drive connector's scope (DRIVE_MCP_CONNECTOR_RFC §3). Both scope strings live here, in composition: the facts
# one is also a Firestore collection name, which core layers must not spell.
DRIVE_SCOPE = "drive_read"
# The external task queue's worker resource (`/tasks-mcp`, EXTERNAL_TASK_QUEUE_RFC §5.4). In SCOPE_OF, so the
# authorization service serves the resource (binds it, mints its tokens); registrable (`valid_scopes`) but never a
# default scope, so a registration that names no scope never gains it.
TASKS_SCOPE = "external_tasks"
# The memory inbox (`/memory-inbox-mcp`, MCP_MEMORY_SUBMISSIONS_RFC §5.1): a write-only resource of its own, registrable
# but never a default scope, as the tasks scope: a facts connector never gains it, the owner adds it as a new connector.
MEMORY_INBOX_SCOPE = "memory_submit"
SCOPE_OF = {MCPResource.FACTS: MCP_DEFAULT_SCOPE, MCPResource.DRIVE: DRIVE_SCOPE, MCPResource.TASKS: TASKS_SCOPE,
            MCPResource.MEMORY_INBOX: MEMORY_INBOX_SCOPE}

# Redirect-URI host allowlist for DCR. Any OAuth client registering via
# /register must supply a redirect_uri whose host matches (or is a
# subdomain of) one of these. claude.ai + claude.com for production,
# localhost/127.0.0.1 for local development and the MCP Inspector tool.
_ALLOWED_REDIRECT_HOSTS = (
    "claude.ai",
    "claude.com",
    "localhost",
    "127.0.0.1",
)


def build_transport_security(resource_uri: str) -> TransportSecuritySettings:
    """DNS-rebinding allowlist for the public host of `resource_uri` (shared by the facts and drive apps).

    FastMCP's default constructor auto-enables DNS rebinding protection whenever `host` is a loopback address,
    which locks the allowlist to 127.0.0.1/localhost/::1. Behind Cloud Run under the public hostname that rejects
    every real request with "Invalid Host header", so the public hostname derived from the resource URI is
    allowlisted instead. The protection stays enabled.
    """
    parsed = urlparse(resource_uri)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            parsed.netloc,
            f"{parsed.netloc}:*",
            "127.0.0.1:*",
            "localhost:*",
        ],
        allowed_origins=[
            f"{parsed.scheme}://{parsed.netloc}",
            f"{parsed.scheme}://{parsed.netloc}:*",
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    )


def _facts_auth_settings(issuer_root: str, resource_uri: str) -> AuthSettings:
    """The facts app's OAuth settings: it is the authorization server for every resource, so its registration offers
    the drive, tasks and memory inbox scopes too. A registration that names no scope gets the defaults, facts and
    drive: the tasks and inbox scopes are never a default, so an existing connector never gains the task queue
    (EXTERNAL_TASK_QUEUE_RFC §5.4) or a write path into memory (MCP_MEMORY_SUBMISSIONS_RFC §5.1)."""
    return AuthSettings(
        issuer_url=AnyHttpUrl(issuer_root),
        resource_server_url=AnyHttpUrl(resource_uri),
        required_scopes=[MCP_DEFAULT_SCOPE],
        # Off on purpose: `verify_access_token` already refuses a token whose `aud` is not this resource
        # (canonicalised). Explicit, so the SDK's default flipping to True in 3.0 changes nothing here.
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[MCP_DEFAULT_SCOPE, DRIVE_SCOPE, TASKS_SCOPE, MEMORY_INBOX_SCOPE],
            default_scopes=[MCP_DEFAULT_SCOPE, DRIVE_SCOPE],
        ),
        # RFC 7009 `/revoke`: the SDK authenticates the client and revokes only that client's own token
        # (`load_refresh_token` checks `client_id`). An access token is a stateless JWT and stays valid until it
        # expires: the provider revokes refresh tokens only.
        revocation_options=RevocationOptions(enabled=True),
    )


@dataclass
class MCPComponents:
    fastmcp: FastMCP
    authorization_service: MCPAuthorizationService
    # The account-lock check of every connector tool call (CABINET_DEVICE_SESSIONS_RFC §7.8): the facts tool uses it,
    # and main.py hands it to the drive connector.
    inactive_users: InactiveUserCheck


def build_mcp_components(
    *,
    db_client,
    env_config: EnvironmentConfig,
    auth_config: AuthConfig,
    memory_search: Callable[[str], Awaitable[Optional[FactsMemoryAgent]]],
    cipher: CipherPort,
    digest_key: bytes,
    user_repo: UserRepository,
    whitelist_repo: WhitelistRepository,
) -> MCPComponents:
    """
    Build the FastMCP server for the remote MCP endpoint.

    Args:
        db_client: Firestore async client
        env_config: EnvironmentConfig (collection names)
        auth_config: AuthConfig (MCP resource URI, TTLs, JWT secret)
        memory_search: resolves a user's memory search agent (`UserAgentFactory.get_memory_search`) —
            the tool handler asks it; None means the account has none
        cipher: the stored-secrets cipher built by build_application_graph
            (never here: main.py swallows this build's errors, so a missing
            key would read as "MCP off", not as a failed start)
        digest_key: the verify-only digest key, built there too
        user_repo / whitelist_repo: read by the authorization service at every refresh rotation; the user is also
            read fresh at every tool call, which refuses a locked account (`inactive_users`)

    Returns:
        MCPComponents with the FastMCP instance and the authorization
        service. main.py separately imports the consent blueprint
        factory from src.web.mcp_consent_app and passes the service to
        it (composition must not import web/).
    """
    # --- storage + service + SDK provider ---
    repo = FirestoreMCPClientRepository(db_client, env_config, cipher=cipher, digest_key=digest_key)

    # Consent URL: same host as the MCP resource URI, at /mcp/consent.
    # When the SDK redirects the browser to our authorize result, the
    # URL must point at the Quart route registered in main_app.
    parsed = urlparse(auth_config.mcp_resource_uri)
    consent_base_url = f"{parsed.scheme}://{parsed.netloc}/mcp/consent"

    mcp_service = MCPAuthorizationService(
        repo=repo,
        jwt_secret=auth_config.oauth_session_secret,
        mcp_resource_uri=auth_config.mcp_resource_uri,
        consent_base_url=consent_base_url,
        allowed_redirect_hosts=_ALLOWED_REDIRECT_HOSTS,
        drive_resource_uri=drive_resource_uri(auth_config.mcp_resource_uri),
        scope_of=SCOPE_OF,
        user_repo=user_repo,
        whitelist_repo=whitelist_repo,
        access_token_ttl=auth_config.mcp_access_token_ttl,
        refresh_token_ttl=auth_config.mcp_refresh_token_ttl,
        auth_code_ttl=auth_config.mcp_auth_code_ttl,
        consent_request_ttl=auth_config.mcp_consent_request_ttl,
    )
    sdk_provider = MCPSdkOAuthProvider(mcp_service)
    inactive_users = InactiveUserCheck(user_repo)

    # --- FastMCP instance with full AS+RS in-process ---
    #
    # Path layout (relative to the server root):
    #   /mcp                                       — MCP protocol endpoint
    #   /authorize, /token, /register              — OAuth AS endpoints
    #   /.well-known/oauth-authorization-server    — AS metadata
    #   /.well-known/oauth-protected-resource/mcp  — PRM metadata (RFC 9728
    #                                                 path-suffix form)
    #
    # issuer_url is the server ROOT, not /mcp, because the SDK registers
    # OAuth AS routes at absolute paths from the issuer's base. If we
    # pointed issuer at /mcp the AS endpoints would be advertised as
    # /mcp/authorize etc. but the SDK would still register them at
    # /authorize — mismatch between metadata and the actual routes.
    #
    # resource_server_url is /mcp, so PRM is at the RFC 9728 path-suffix
    # location and WWW-Authenticate points there correctly.
    parsed_resource = urlparse(auth_config.mcp_resource_uri)
    issuer_root = f"{parsed_resource.scheme}://{parsed_resource.netloc}"

    transport_security = build_transport_security(auth_config.mcp_resource_uri)

    fastmcp = FastMCP(
        name="alekbot",
        instructions="alekbot exocortex — retrieve user's memory facts via get_user_context.",
        # streamable_http_path defaults to "/mcp" — keep it.
        auth_server_provider=sdk_provider,
        auth=_facts_auth_settings(issuer_root, auth_config.mcp_resource_uri),
        transport_security=transport_security,
        stateless_http=True,  # Cloud Run may route subsequent requests to different instances
    )

    register_facts_tool(fastmcp, mcp_service=mcp_service, inactive_users=inactive_users, memory_search=memory_search)

    logger.info(
        f"🧠 MCP components built: resource_uri={auth_config.mcp_resource_uri}, "
        f"consent_url={consent_base_url}"
    )
    return MCPComponents(
        fastmcp=fastmcp,
        authorization_service=mcp_service,
        inactive_users=inactive_users,
    )
