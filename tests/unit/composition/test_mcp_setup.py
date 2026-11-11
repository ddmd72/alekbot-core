"""
Remote MCP server tool contract: `get_user_context`.

Two concerns, both previously untested (F16.3):

1. The JSON Schema emitted in `tools/list`. External MCP clients build
   their first tool call from this schema alone, so it must carry plain
   types (no `anyOf`/`null` unions, which clients collapse to `any`) and
   a per-parameter `description` — prose in the tool description is not
   read reliably at argument-construction time.

2. Liberal input coercion. Every shape a client plausibly sends must
   reach the memory search agent instead of failing pydantic validation:
   a validation failure surfaces to the calling model as `isError` and
   costs a full extra round-trip.

The `queries` parameter, the agent call and the result rendering are in
`test_mcp_get_user_context_v2.py`; the search itself is tested with the
memory search agent; the normalization rules in
`tests/unit/domain/test_mcp_normalize_queries.py`.
"""

import json
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from src.composition.mcp_setup import build_mcp_components
from src.domain.fact_search_result import PhraseSearchResult
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.mcp_gate_fakes import allowing_repos
from src.composition.mcp_sdk_oauth_provider import AlekAccessToken


_TOOL_NAME = "get_user_context"
_RESOURCE_URI = "https://mcp.example.test/mcp"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _auth_config() -> MagicMock:
    cfg = MagicMock()
    cfg.mcp_resource_uri = _RESOURCE_URI
    cfg.oauth_session_secret = "x" * 48
    cfg.mcp_access_token_ttl = 3600
    cfg.mcp_refresh_token_ttl = 2592000
    cfg.mcp_auth_code_ttl = 600
    cfg.mcp_consent_request_ttl = 600
    return cfg


class RecordingMemoryAgent:
    """The user's memory search agent: serves the token's account, records every phrase list it is asked."""

    account_id = "acc-42"

    def __init__(self) -> None:
        self.calls: list = []

    async def search_facts(self, queries, *, path):
        self.calls.append(list(queries))
        return PhraseSearchResult((), ())


@pytest.fixture
def memory_agent() -> RecordingMemoryAgent:
    return RecordingMemoryAgent()


@pytest.fixture
def components(memory_agent):
    async def memory_search(user_id):
        return memory_agent

    return build_mcp_components(
        db_client=MagicMock(),
        env_config=MagicMock(),
        auth_config=_auth_config(),
        memory_search=memory_search,
        cipher=AesGcmTestCipher(),
        digest_key=bytes(range(32)),
        user_repo=allowing_repos("u1")[0],
        whitelist_repo=allowing_repos("u1")[1],
    )


@pytest.fixture
def tool(components):
    return components.fastmcp._tool_manager.get_tool(_TOOL_NAME)


@pytest.fixture
def authenticated_ctx():
    """
    Minimal stand-in for the FastMCP Context the handler reads.

    Only `ctx.request_context.request.user.access_token` is touched.
    """
    token = AlekAccessToken(
        token="jwt",
        client_id="mcp-test",
        scopes=["user_context"],
        expires_at=4102444800,
        resource=_RESOURCE_URI,
        user_id="user-42",
        account_id="acc-42",
    )
    ctx = MagicMock()
    ctx.request_context.request.user.access_token = token
    return ctx


async def _schema(components) -> dict:
    tools = await components.fastmcp.list_tools()
    matching = [t for t in tools if t.name == _TOOL_NAME]
    assert matching, f"{_TOOL_NAME} not registered"
    return matching[0].inputSchema


# ---------------------------------------------------------------------------
# Emitted JSON Schema
# ---------------------------------------------------------------------------


class TestInputSchema:
    async def test_no_null_type_anywhere(self, components):
        assert '"null"' not in json.dumps(await _schema(components))

    async def test_only_queries_is_required(self, components):
        assert (await _schema(components))["required"] == ["queries"]

    async def test_no_validation_constraints_are_advertised(self, components):
        """
        FastMCP validates the schema with pydantic, so any advertised
        constraint turns a recoverable input into a failed tool call.
        Bounds are documented in the descriptions and enforced by
        normalization instead.
        """
        dumped = json.dumps(await _schema(components))

        for constraint in ("minLength", "maxLength", "maxItems", "pattern"):
            assert constraint not in dumped

    async def test_additional_properties_are_not_forbidden(self, components):
        """An unknown extra argument must be ignored, never rejected."""
        assert (await _schema(components)).get("additionalProperties") is not False


# ---------------------------------------------------------------------------
# Liberal input coercion (through the real pydantic validation stack)
# ---------------------------------------------------------------------------


class TestArgumentCoercion:
    async def test_unknown_argument_is_ignored(
        self, tool, authenticated_ctx, memory_agent
    ):
        await tool.run(
            {"queries": ["Alex owns a 2015 Skoda Fabia"], "hypothetical_param": 1},
            context=authenticated_ctx,
            convert_result=False,
        )

        assert memory_agent.calls == [["Alex owns a 2015 Skoda Fabia"]]

    async def test_non_english_query_is_accepted_verbatim(
        self, tool, authenticated_ctx, memory_agent
    ):
        await tool.run(
            {"queries": ["  o passaporte da Clara Moreira  "]},
            context=authenticated_ctx,
            convert_result=False,
        )

        assert memory_agent.calls == [["o passaporte da Clara Moreira"]]


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------


class TestErrorContract:
    async def test_missing_authenticated_user_raises(self, tool, memory_agent):
        ctx = MagicMock()
        ctx.request_context.request = None

        with pytest.raises(ToolError):
            await tool.run({"queries": ["q"]}, context=ctx, convert_result=False)

        assert memory_agent.calls == []

    async def test_wrong_access_token_type_raises(self, tool, memory_agent):
        ctx = MagicMock()
        ctx.request_context.request.user.access_token = object()

        with pytest.raises(ToolError):
            await tool.run({"queries": ["q"]}, context=ctx, convert_result=False)

        assert memory_agent.calls == []
