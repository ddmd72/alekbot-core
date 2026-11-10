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

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable, List, Optional
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
from ..agents.memory_search_agent import FactsMemoryAgent
from ..domain import fact_domains
from ..domain.fact_when import when_line
from ..domain.mcp import normalize_queries, queries_dropped
from ..domain.memory_search_texts import QUERIES_FIELD_TEXT
from ..domain.mcp_resource import MCPResource, drive_resource_uri
from ..domain.request_context import RequestContext
from ..domain.voice_mcp_token import FACTS_TOOL, VOICE_RESULT_MAX_CHARS, McpTokenKind, tool_permitted
from ..ports.cipher_port import CipherPort
from ..ports.user_repository import UserRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..services.mcp_authorization_service import MCPAuthorizationService
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
    "Looks up what the user's exocortex (alekbot) knows about the user: the facts of their life, the people around "
    "them, places, plans, preferences, past events and decisions. Each stored record is a short English statement "
    "about one matter of the user's life. Every phrase you send is turned into a meaning vector and compared with the "
    "records' statements (semantic vector search); the records closest to any of your phrases come back with their "
    "dates.\n"
    "\n"
    "Call it whenever personal context could change the content or tone of your answer, including when you are not "
    "sure it is relevant. Skip it only for questions with no personal side at all. An empty result means nothing is "
    "stored on that matter; it is an answer, not an error."
)

# The parameter description (QUERIES_FIELD_TEXT) is what a client reads when
# constructing arguments. The bound (1-4 phrases) is stated there as guidance
# only — it is NOT advertised as a JSON Schema constraint, because FastMCP
# validates the schema with pydantic and a violation would fail the call
# instead of being silently normalized (see src/domain/mcp.py).

# The only rejection: nothing usable was sent.
_NO_QUERIES = "No search phrases supplied. Pass one to four English phrases as queries."
_NO_MEMORY_SEARCH = "Memory search is not available for this account."

# Zero results is a successful answer, not an error and not an instruction
# to the model to try again — a "rephrase and retry" hint costs a round-trip
# and the server has no better phrasing to offer than the caller did.
_NO_RESULTS = "No records matched."

# A tool outside the token's tools claim (LELIK_VOICE_MCP_RFC §5.3): one neutral line, nothing read.
_REFUSED = "Refused: this tool is not available to this caller."
# The last line of a voice result cut at VOICE_RESULT_MAX_CHARS (§8, O5).
_VOICE_OVER_SIZE = "[More records matched than fit here; ask a narrower question to see the rest.]"


def _format_facts(facts) -> str:
    """Facts grouped under the English label of their primary domain, each with its `when:` line when it has one."""
    if not facts:
        return _NO_RESULTS

    by_group: dict[str, list] = {}
    for f in facts:
        primary = getattr(f, "primary_domain", None)
        group = (fact_domains.label(primary, "en") if primary else None) or getattr(f, "domain", None) or "general"
        group = getattr(group, "value", group)
        by_group.setdefault(str(group), []).append(f)

    lines: List[str] = []
    for group in sorted(by_group.keys()):
        lines.append(f"## {group}")
        for f in by_group[group]:
            when = when_line(f)
            lines.append(f"- {f.text}" + (f" ({when})" if when else ""))
        lines.append("")
    return "\n".join(lines).rstrip()


# The phrases the validator cut beyond the limit, for the handler's log line. Validation and the handler run in
# one task, so a ContextVar carries the count between them.
_DROPPED: ContextVar[int] = ContextVar("mcp_queries_dropped", default=0)


def _normalize_and_count(value):
    """normalize_queries, remembering how many usable phrases it left out."""
    _DROPPED.set(queries_dropped(value))
    return normalize_queries(value)


def _agent_account_id(agent) -> Optional[str]:
    """The tenant a memory agent serves, from its public `account_id`."""
    return getattr(agent, "account_id", None)


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
    voice_tokens: Optional[VoiceMcpTokenService] = None,
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
        queries: Annotated[
            List[str],
            BeforeValidator(_normalize_and_count),
            Field(description=QUERIES_FIELD_TEXT),
        ],
        ctx: Context,
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
        if not queries:
            raise ToolError(_NO_QUERIES)

        dropped = _DROPPED.get()
        cut = f" dropped={dropped}" if dropped else ""
        logger.info(f"🔎 MCP get_user_context: user={user_id[:8]} queries={queries!r}{cut}{client}")

        with RequestContext(user_id=user_id, account_id=account_id):
            agent = await memory_search(user_id)
            if agent is None:
                logger.warning(f"MCP get_user_context: user={user_id[:8]} has no memory search agent{client}")
                raise ToolError(_NO_MEMORY_SEARCH)
            if _agent_account_id(agent) != account_id:
                logger.warning(
                    f"MCP get_user_context: user={user_id[:8]} memory agent serves another account than the token's; "
                    f"refused{client}"
                )
                raise ToolError(_NO_MEMORY_SEARCH)
            facts = await agent.search_facts(queries, path="connector_voice" if voice else "connector")

        result = _format_facts(facts)
        if voice:
            result = cap_result(result, _VOICE_OVER_SIZE, limit=VOICE_RESULT_MAX_CHARS)
        logger.info(f"✅ MCP get_user_context: user={user_id[:8]} facts={len(facts)} len={len(result)}{client}")
        return result

    logger.info(
        f"🧠 MCP components built: resource_uri={auth_config.mcp_resource_uri}, "
        f"consent_url={consent_base_url}"
    )
    return MCPComponents(
        fastmcp=fastmcp,
        authorization_service=mcp_service,
    )
