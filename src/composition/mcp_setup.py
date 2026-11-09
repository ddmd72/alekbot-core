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
from typing import Annotated, List, Optional
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, BeforeValidator, Field

from ..adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..domain.drive_connector_result import cap_result
from ..domain.mcp import KEYWORDS_MAX, normalize_keywords, normalize_phrase
from ..domain.mcp_resource import MCPResource, drive_resource_uri
from ..domain.request_context import RequestContext
from ..domain.voice_mcp_token import FACTS_TOOL, VOICE_RESULT_MAX_CHARS, McpTokenKind, tool_permitted
from ..ports.cipher_port import CipherPort
from ..ports.user_repository import UserRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..services.mcp_authorization_service import MCPAuthorizationService
from ..services.search_enrichment_service import SearchEnrichmentService
from ..services.voice_mcp_token_service import VoiceMcpTokenService
from ..utils.logger import logger
from .mcp_sdk_oauth_provider import AlekAccessToken, MCPSdkOAuthProvider


# OAuth scope advertised by the MCP server to claude.ai. Deliberately
# namespaced to avoid collision with the Firestore `user_context`
# collection name (which arch test REQ-ARCH-21 guards against in core
# layers). Composition is free of that restriction.
MCP_DEFAULT_SCOPE = "user_context"
# The drive connector's scope (DRIVE_MCP_CONNECTOR_RFC §3). Both scope strings live here, in composition: the facts
# one is also a Firestore collection name, which core layers must not spell.
DRIVE_SCOPE = "drive_read"
SCOPE_OF = {MCPResource.FACTS: MCP_DEFAULT_SCOPE, MCPResource.DRIVE: DRIVE_SCOPE}

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

# A tool outside the token's tools claim (LELIK_VOICE_MCP_RFC §5.3): one neutral line, nothing read.
_REFUSED = "Refused: this tool is not available to this caller."
# The last line of a voice result cut at VOICE_RESULT_MAX_CHARS (§8, O5).
_VOICE_OVER_SIZE = "[More records matched than fit here; ask a narrower question to see the rest.]"


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
    cipher: CipherPort,
    digest_key: bytes,
    user_repo: UserRepository,
    whitelist_repo: WhitelistRepository,
    voice_tokens: Optional[VoiceMcpTokenService] = None,
) -> MCPComponents:
    """
    Build the FastMCP server for the remote MCP endpoint.

    Args:
        db_client: Firestore async client
        env_config: EnvironmentConfig (collection names)
        auth_config: AuthConfig (MCP resource URI, TTLs, JWT secret)
        search_enrichment_service: the RRF memory search service —
            invoked by the tool handler
        cipher: the stored-secrets cipher built by build_application_graph
            (never here: main.py swallows this build's errors, so a missing
            key would read as "MCP off", not as a failed start)
        digest_key: the verify-only digest key, built there too
        user_repo / whitelist_repo: read by the authorization service at every refresh rotation
        voice_tokens: the voice token kind's verifier (LELIK_VOICE_MCP_RFC §5.3); None refuses every voice token

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
    sdk_provider = MCPSdkOAuthProvider(mcp_service, voice_tokens)

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
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer_root),
            resource_server_url=AnyHttpUrl(auth_config.mcp_resource_uri),
            required_scopes=[MCP_DEFAULT_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[MCP_DEFAULT_SCOPE, DRIVE_SCOPE],
                default_scopes=[MCP_DEFAULT_SCOPE, DRIVE_SCOPE],
            ),
        ),
        transport_security=transport_security,
        stateless_http=True,  # Cloud Run may route subsequent requests to different instances
    )

    # --- Tool: get_user_context ---
    @fastmcp.tool(
        name=FACTS_TOOL,
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

        user_id = access_token.user_id
        account_id = access_token.account_id
        # A voice token (LELIK_VOICE_MCP_RFC §5.3-§5.4): its tools claim is enforced, its search terms are logged like
        # an OAuth call's (owner: without them a voice miss cannot be diagnosed), its result takes the voice cap.
        voice = access_token.kind is McpTokenKind.VOICE
        client = " client=voice" if voice else ""

        if not tool_permitted(access_token.kind, access_token.tools, FACTS_TOOL):
            logger.warning(f"MCP get_user_context: user={user_id[:8]} refused: tool outside the token's claim{client}")
            return _REFUSED

        # The only legitimate input rejection: nothing to search on at all.
        # This is also the only branch allowed to instruct the model, because
        # it is the only one the server cannot resolve on its own.
        if not query and not alternate_phrasing and not keywords:
            raise ToolError(
                "No search terms supplied. Pass the user's information need "
                "as `query`, a short English phrase."
            )

        logger.info(
            f"🔎 MCP get_user_context: user={user_id[:8]} "
            f"query={query!r} "
            f"alternate_phrasing={alternate_phrasing!r} "
            f"keywords={keywords!r}{client}"
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
        if voice:
            result = cap_result(result, _VOICE_OVER_SIZE, limit=VOICE_RESULT_MAX_CHARS)
        logger.info(
            f"✅ MCP get_user_context: user={user_id[:8]} "
            f"facts={len(enriched.facts)} len={len(result)}{client}"
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
