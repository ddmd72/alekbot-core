"""
Composition helper: the facts connector's one tool, `get_user_context`.

Registered on the facts FastMCP app by `register_facts_tool`; `build_mcp_components` (mcp_setup) calls it. Each call
checks the account lock, then (for an OAuth token) the grant, as the tasks and inbox apps do, then asks the user's
memory search agent. A failure is answered by its type only: an exception's message can carry the user's words.
"""

from contextvars import ContextVar
from typing import Annotated, Awaitable, Callable, List, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BeforeValidator, Field

from ..agents.memory_search_agent import FactsMemoryAgent
from ..domain.mcp import ACCOUNT_LOCKED_TOOL_ERROR, GRANT_REFUSED_TOOL_ERROR, normalize_queries, queries_dropped
from ..domain.memory_search_format import format_search_result
from ..domain.memory_search_texts import NO_RECORDS_TEXT, QUERIES_FIELD_TEXT
from ..domain.request_context import RequestContext
from ..domain.voice_mcp_token import McpTokenKind
from ..services.inactive_user_check import InactiveUserCheck
from ..services.mcp_authorization_service import MCPAuthorizationService
from ..utils.logger import logger
from .mcp_sdk_oauth_provider import AlekAccessToken


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
    "about one matter of the user's life. Every phrase you send is read for the people, places and times it names and "
    "compared by meaning with the records' statements; the records that match best come back with their dates, the "
    "first phrase's matches first.\n"
    "\n"
    "Call it whenever personal context could change the content or tone of your answer, including when you are not "
    "sure it is relevant. Skip it only for questions with no personal side at all. An empty result means nothing is "
    "stored on that matter; it is an answer, not an error.\n"
    "\n"
    "The records returned here are already in the user's memory: never send them back into it through any tool, as "
    "they are or reworded, also when the user only agrees with them. A memory fed its own records confirms itself and "
    "fills with duplicates; only what the user tells or shows you, including what they add to a record or change in "
    "it, and what you conclude from that, is new to it."
)
# The last paragraph is the memory inbox's loop guard (MCP_MEMORY_SUBMISSIONS_RFC §5.5): the same sentence as the inbox
# tool's, worded to hold for a client that has no inbox connector. A git constant: it ships by deploy, and claude.ai
# shows it after the facts connector is re-added (it caches `tools/list`).

# The parameter description (QUERIES_FIELD_TEXT) is what a client reads when
# constructing arguments. The bound (1-4 phrases) is stated there as guidance
# only — it is NOT advertised as a JSON Schema constraint, because FastMCP
# validates the schema with pydantic and a violation would fail the call
# instead of being silently normalized (see src/domain/mcp.py).

# The only rejection: nothing usable was sent.
_NO_QUERIES = "No search phrases supplied. Pass one to four English phrases as queries, most important first."
_NO_MEMORY_SEARCH = "Memory search is not available for this account."

# Zero results is a successful answer, not an error and not an instruction
# to the model to try again — a "rephrase and retry" hint costs a round-trip
# and the server has no better phrasing to offer than the caller did.
_NO_RESULTS = NO_RECORDS_TEXT

# The facts tool's name (the facts app registers it under this name).
FACTS_TOOL = "get_user_context"


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



def register_facts_tool(
    fastmcp: FastMCP,
    *,
    mcp_service: MCPAuthorizationService,
    inactive_users: InactiveUserCheck,
    memory_search: Callable[[str], Awaitable[Optional[FactsMemoryAgent]]],
) -> None:
    """Register `get_user_context` on the facts app."""
    # --- Tool: get_user_context ---
    async def _answer(
        # Accept liberally, advertise strictly. The annotations below emit
        # plain `string` / `array<string>` with no `anyOf` union (clients
        # collapse unions to an untyped value and then guess the shape),
        # while the BeforeValidators absorb every shape a client plausibly
        # sends — explicit nulls, one string (kept as one phrase), a JSON-encoded array.
        # A rejected argument costs a full extra round-trip with the calling
        # model, so nothing that can be interpreted is treated as an error.
        queries: Annotated[
            List[str],
            BeforeValidator(_normalize_and_count),
            Field(description=QUERIES_FIELD_TEXT),
        ],
        ctx: Context,
    ) -> str:
        """The tool's body; `get_user_context` below runs it and answers a failure by its type only."""
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

        # The account lock (CABINET_DEVICE_SESSIONS_RFC §7.8): an access token issued before it verifies until it
        # expires; the stored user, read fresh, refuses the call.
        if await inactive_users.is_inactive(user_id):
            logger.warning(f"🔒 MCP get_user_context: user={user_id[:8]} is inactive (account lock), refused")
            raise ToolError(ACCOUNT_LOCKED_TOOL_ERROR)

        # The grant is re-checked on every call (as the tasks and inbox apps): a grant revoked in the Cabinet, or a
        # user taken off the whitelist, is refused on the next call. A voice token is no OAuth
        # grant: it has its own lifetime and revocation.
        if access_token.kind is McpTokenKind.OAUTH and not await mcp_service.grant_allowed(
            user_id, access_token.grant_id
        ):
            logger.warning(f"🔒 MCP get_user_context: user={user_id[:8]} grant not allowed, refused")
            raise ToolError(GRANT_REFUSED_TOOL_ERROR)

        # The only legitimate input rejection: nothing to search on at all.
        # This is also the only branch allowed to instruct the model, because
        # it is the only one the server cannot resolve on its own.
        if not queries:
            raise ToolError(_NO_QUERIES)

        dropped = _DROPPED.get()
        cut = f" dropped={dropped}" if dropped else ""
        # counts and lengths only: the phrases are the user's words
        logger.info(
            f"🔎 MCP get_user_context: user={user_id[:8]} queries={len(queries)} "
            f"lengths={[len(q) for q in queries]}{cut}"
        )

        with RequestContext(user_id=user_id, account_id=account_id):
            agent = await memory_search(user_id)
            if agent is None:
                logger.warning(f"MCP get_user_context: user={user_id[:8]} has no memory search agent")
                raise ToolError(_NO_MEMORY_SEARCH)
            if _agent_account_id(agent) != account_id:
                logger.warning(
                    f"MCP get_user_context: user={user_id[:8]} memory agent serves another account than the token's; "
                    f"refused"
                )
                raise ToolError(_NO_MEMORY_SEARCH)
            found = await agent.search_facts(queries, path="connector")

        result = format_search_result(queries, found)
        logger.info(f"✅ MCP get_user_context: user={user_id[:8]} facts={len(found.facts)} len={len(result)}")
        return result

    @fastmcp.tool(
        name=FACTS_TOOL,
        title="Get user context from alekbot memory",
        description=_TOOL_DESCRIPTION,
    )
    async def get_user_context(
        queries: Annotated[
            List[str],
            BeforeValidator(_normalize_and_count),
            Field(description=QUERIES_FIELD_TEXT),
        ],
        ctx: Context,
    ) -> str:
        try:
            return await _answer(queries, ctx)
        except ToolError:
            raise
        except Exception as exc:
            # the type only: an exception's message can carry the user's words
            logger.error(f"❌ MCP get_user_context failed: {type(exc).__name__}")
            raise ToolError(f"{FACTS_TOOL} failed: {type(exc).__name__}; try again later.") from None
