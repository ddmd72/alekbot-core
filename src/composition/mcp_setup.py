"""
Composition helper: wire the remote MCP server (FastMCP + OAuth
provider) into existing infrastructure.

This module is the single place that knows about all layers:
- AuthConfig (config)
- FirestoreMCPClientRepository (adapter)
- MCPAuthorizationService (service)
- MCPSdkOAuthProvider (composition)
- FastMCP (SDK)
- SearchEnrichmentService (service) — the actual tool implementation

It exposes one function, `build_mcp_components`, which returns a
FastMCP instance (ready to be mounted via `streamable_http_app()`) and
the authorization service (which main.py passes to the consent
blueprint factory — the consent blueprint cannot be imported here
because composition/ must not depend on web/).
"""

from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import Context, FastMCP
from pydantic import AnyHttpUrl

from ..adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..domain.request_context import RequestContext
from ..services.mcp_authorization_service import MCPAuthorizationService
from ..services.search_enrichment_service import SearchEnrichmentService
from ..utils.logger import logger
from .mcp_sdk_oauth_provider import AlekAccessToken, MCPSdkOAuthProvider


# OAuth scope advertised by the MCP server to claude.ai. Deliberately
# namespaced to avoid collision with the Firestore `user_context`
# collection name (which arch test REQ-ARCH-21 guards against in core
# layers). Composition is free of that restriction.
MCP_DEFAULT_SCOPE = "user_context"

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


@dataclass
class MCPComponents:
    fastmcp: FastMCP
    authorization_service: MCPAuthorizationService


# The tool description is load-bearing — it's the only lever we have on
# claude.ai's tool-use decisions. Be concrete, state when to use it, and
# when to skip. See Anthropic's tool-writing guidance.
_TOOL_DESCRIPTION = (
    "ALWAYS call this tool before answering any question from the user. "
    "Retrieves the user's personal biographical facts, preferences, ongoing "
    "projects, opinions, and historical context from their exocortex "
    "(alekbot). Without this context you will miss critical information "
    "the user expects you to know. Pass the user's question as `query`; "
    "optionally add `alternate_phrasing` with synonyms for better recall "
    "and `keywords` with 2-5 topical tags. Skip only for pure math/code "
    "questions with zero personal dimension."
)


def _format_enriched_facts(facts) -> str:
    if not facts:
        return "(no memory facts found for this query)"

    lines: List[str] = []
    by_domain: dict[str, list] = {}
    for f in facts:
        key = (f.domain or "general")
        by_domain.setdefault(key, []).append(f)

    for domain in sorted(by_domain.keys()):
        lines.append(f"## {domain}")
        for f in by_domain[domain]:
            score = f" (score={f.relevance_score:.3f})" if f.relevance_score else ""
            lines.append(f"- {f.content}{score}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_mcp_components(
    *,
    db_client,
    env_config: EnvironmentConfig,
    auth_config: AuthConfig,
    search_enrichment_service: SearchEnrichmentService,
) -> MCPComponents:
    """
    Build the FastMCP server for the remote MCP endpoint.

    Args:
        db_client: Firestore async client
        env_config: EnvironmentConfig (collection names)
        auth_config: AuthConfig (MCP resource URI, TTLs, JWT secret)
        search_enrichment_service: the RRF memory search service —
            invoked by the tool handler

    Returns:
        MCPComponents with the FastMCP instance and the authorization
        service. main.py separately imports the consent blueprint
        factory from src.web.mcp_consent_app and passes the service to
        it (composition must not import web/).
    """
    # --- storage + service + SDK provider ---
    repo = FirestoreMCPClientRepository(db_client, env_config)

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
        access_token_ttl=auth_config.mcp_access_token_ttl,
        refresh_token_ttl=auth_config.mcp_refresh_token_ttl,
        auth_code_ttl=auth_config.mcp_auth_code_ttl,
        consent_request_ttl=auth_config.mcp_consent_request_ttl,
    )
    sdk_provider = MCPSdkOAuthProvider(mcp_service)

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

    fastmcp = FastMCP(
        name="alekbot",
        instructions="alekbot exocortex — retrieve user's memory facts via get_user_context.",
        # streamable_http_path defaults to "/mcp" — keep it.
        auth_server_provider=sdk_provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer_root),
            resource_server_url=AnyHttpUrl(auth_config.mcp_resource_uri),
            required_scopes=[MCP_DEFAULT_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[MCP_DEFAULT_SCOPE],
                default_scopes=[MCP_DEFAULT_SCOPE],
            ),
        ),
        stateless_http=True,  # Cloud Run may route subsequent requests to different instances
    )

    # --- Tool: get_user_context ---
    @fastmcp.tool(
        name="get_user_context",
        title="Get user context from alekbot memory",
        description=_TOOL_DESCRIPTION,
    )
    async def get_user_context(
        query: str,
        ctx: Context,
        alternate_phrasing: Optional[str] = None,
        keywords: Optional[List[str]] = None,
    ) -> str:
        # Extract authenticated user from the SDK access token subclass
        request = ctx.request_context.request
        if request is None or getattr(request, "user", None) is None:
            logger.warning("MCP get_user_context: no authenticated user on request")
            return "(authentication error — no user context available)"

        access_token = getattr(request.user, "access_token", None)
        if not isinstance(access_token, AlekAccessToken):
            logger.warning(
                f"MCP get_user_context: unexpected access token type {type(access_token)}"
            )
            return "(authentication error — invalid token type)"

        user_id = access_token.user_id
        account_id = access_token.account_id

        logger.info(
            f"🔎 MCP get_user_context: user={user_id[:8]} query={query[:80]!r}"
        )

        with RequestContext(user_id=user_id, account_id=account_id):
            enriched = await search_enrichment_service.enrich_context(
                keywords=keywords or [],
                search_phrase_1=query,
                search_phrase_2=alternate_phrasing or query,
                dedup_threshold=0.98,
                skip_semantic_dedup=False,
            )

        result = _format_enriched_facts(enriched.facts)
        logger.info(
            f"✅ MCP get_user_context: user={user_id[:8]} "
            f"facts={len(enriched.facts)} len={len(result)}"
        )
        return result

    logger.info(
        f"🧠 MCP components built: resource_uri={auth_config.mcp_resource_uri}, "
        f"consent_url={consent_base_url}"
    )
    return MCPComponents(
        fastmcp=fastmcp,
        authorization_service=mcp_service,
    )
