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
from typing import Annotated, List
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, BeforeValidator, Field

from ..adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..domain.mcp import KEYWORDS_MAX, normalize_keywords, normalize_phrase
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
#
# Deliberately NOT an absolute imperative ("ALWAYS call this tool before
# answering any question"): a hard rule here competes with the user's own
# preferences on the client side, and the observed effect was a model
# oscillating between over-calling and improvising arguments.
_TOOL_DESCRIPTION = (
    "Retrieves the user's stored personal context from their exocortex "
    "(alekbot): biographical facts, preferences, ongoing projects, opinions "
    "and historical records.\n"
    "\n"
    "WHEN TO CALL\n"
    "Call whenever personal context could change the content or tone of the "
    "answer, including when you are unsure whether it is relevant. Skip only "
    "for purely technical or mathematical questions with zero personal "
    "dimension.\n"
    "\n"
    "RETRIEVAL\n"
    "Multi-vector RRF search over `query`, `alternate_phrasing` and "
    "`keywords`. Supplying all three materially improves recall. Records are "
    "stored in English: build `query` and `keywords` in English regardless of "
    "the language the user wrote in.\n"
    "\n"
    "RETURNS\n"
    "Matching records grouped by category, as markdown. An empty result means "
    "no records matched — that is a valid answer, not an error."
)

# Per-parameter descriptions. These are what a client actually reads when
# constructing arguments; prose in the tool description above is not a
# reliable substitute. Bounds are stated here as guidance only — they are
# NOT advertised as JSON Schema constraints, because FastMCP validates the
# schema with pydantic and a violation would fail the call instead of being
# silently normalized (see src/domain/mcp.py).
_QUERY_DESCRIPTION = (
    "The user's information need, restated in English as a short noun phrase "
    'or statement. Example: "MCP connector configuration and tool schema '
    'issues".'
)

_ALTERNATE_PHRASING_DESCRIPTION = (
    "A distinct rephrasing of `query` using different vocabulary, used as a "
    "second retrieval vector. If the user's question was not in English, put "
    'the original-language phrasing here. Example: "problems with the '
    'claude.ai custom connector schema". Omit or pass an empty string to '
    "disable this vector."
)

_KEYWORDS_DESCRIPTION = (
    f"2-{KEYWORDS_MAX} single-word topical tags, lowercase English, used as a "
    'third retrieval vector. Example: ["mcp", "schema", "connector"]. Tags '
    f"beyond the first {KEYWORDS_MAX} are ignored. Omit or pass an empty array "
    "to disable this vector."
)

# Zero results is a successful answer, not an error and not an instruction
# to the model to try again — a "rephrase and retry" hint costs a round-trip
# and the server has no better phrasing to offer than the caller did.
_NO_RESULTS = "No records matched."


def _format_enriched_facts(facts) -> str:
    if not facts:
        return _NO_RESULTS

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

    # Transport security: FastMCP's default constructor auto-enables DNS
    # rebinding protection whenever `host` is a loopback address, which
    # locks the allowed_hosts allowlist to 127.0.0.1/localhost/::1. Since
    # we serve behind Cloud Run + Google Frontend under the public
    # hostname `dev.alekbot.app`, that default rejects every real request
    # with "Invalid Host header". Allowlist the public hostname(s) derived
    # from mcp_resource_uri instead. DNS rebinding protection remains
    # enabled — we just extend the allow-list.
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            parsed_resource.netloc,
            f"{parsed_resource.netloc}:*",
            "127.0.0.1:*",
            "localhost:*",
        ],
        allowed_origins=[
            f"{parsed_resource.scheme}://{parsed_resource.netloc}",
            f"{parsed_resource.scheme}://{parsed_resource.netloc}:*",
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    )

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
        transport_security=transport_security,
        stateless_http=True,  # Cloud Run may route subsequent requests to different instances
    )

    # --- Tool: get_user_context ---
    @fastmcp.tool(
        name="get_user_context",
        title="Get user context from alekbot memory",
        description=_TOOL_DESCRIPTION,
    )
    async def get_user_context(
        # Accept liberally, advertise strictly. The annotations below emit
        # plain `string` / `array<string>` with no `anyOf` union (clients
        # collapse unions to an untyped value and then guess the shape),
        # while the BeforeValidators absorb every shape a client plausibly
        # sends — explicit nulls, a delimited string, a JSON-encoded array.
        # A rejected argument costs a full extra round-trip with the calling
        # model, so nothing that can be interpreted is treated as an error.
        query: Annotated[
            str,
            BeforeValidator(normalize_phrase),
            Field(description=_QUERY_DESCRIPTION),
        ],
        ctx: Context,
        alternate_phrasing: Annotated[
            str,
            BeforeValidator(normalize_phrase),
            Field(description=_ALTERNATE_PHRASING_DESCRIPTION),
        ] = "",
        keywords: Annotated[
            List[str],
            BeforeValidator(normalize_keywords),
            Field(description=_KEYWORDS_DESCRIPTION),
        ] = [],  # noqa: B006 — never mutated; pydantic copies the default
    ) -> str:
        # Extract authenticated user from the SDK access token subclass
        request = ctx.request_context.request
        if request is None or getattr(request, "user", None) is None:
            logger.warning("MCP get_user_context: no authenticated user on request")
            raise ToolError("Authentication failed: no user context on this request.")

        access_token = getattr(request.user, "access_token", None)
        if not isinstance(access_token, AlekAccessToken):
            logger.warning(
                f"MCP get_user_context: unexpected access token type {type(access_token)}"
            )
            raise ToolError("Authentication failed: invalid access token.")

        # The only legitimate input rejection: nothing to search on at all.
        # This is also the only branch allowed to instruct the model, because
        # it is the only one the server cannot resolve on its own.
        if not query and not alternate_phrasing and not keywords:
            raise ToolError(
                "No search terms supplied. Pass the user's information need "
                "as `query`, a short English phrase."
            )

        user_id = access_token.user_id
        account_id = access_token.account_id

        logger.info(
            f"🔎 MCP get_user_context: user={user_id[:8]} "
            f"query={query!r} "
            f"alternate_phrasing={alternate_phrasing!r} "
            f"keywords={keywords!r}"
        )

        with RequestContext(user_id=user_id, account_id=account_id):
            enriched = await search_enrichment_service.enrich_context(
                keywords=keywords,
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
