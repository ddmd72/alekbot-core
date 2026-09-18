"""
Remote MCP server tool contract: `get_user_context`.

Two concerns, both previously untested (F16.3):

1. The JSON Schema emitted in `tools/list`. External MCP clients build
   their first tool call from this schema alone, so it must carry plain
   types (no `anyOf`/`null` unions, which clients collapse to `any`) and
   a per-parameter `description` — prose in the tool description is not
   read reliably at argument-construction time.

2. Liberal input coercion. Every shape a client plausibly sends must
   reach the search service instead of failing pydantic validation: a
   validation failure surfaces to the calling model as `isError` and
   costs a full extra round-trip.

Business logic of the search itself lives in
`tests/unit/services/test_search_enrichment_service.py`; the
normalization rules in `tests/unit/domain/test_mcp_tool_input.py`.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from src.composition.mcp_setup import build_mcp_components
from src.composition.mcp_sdk_oauth_provider import AlekAccessToken
from src.domain.search import EnrichedContext, EnrichedFact
from src.services.search_enrichment_service import SearchEnrichmentService


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


@pytest.fixture
def enrichment_service() -> AsyncMock:
    svc = AsyncMock(spec=SearchEnrichmentService)
    svc.enrich_context.return_value = EnrichedContext(
        facts=[],
        total_sources=0,
        dedup_count=0,
        biographical_dedup_count=0,
    )
    return svc


@pytest.fixture
def components(enrichment_service):
    return build_mcp_components(
        db_client=MagicMock(),
        env_config=MagicMock(),
        auth_config=_auth_config(),
        search_enrichment_service=enrichment_service,
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
    async def test_no_anyof_unions(self, components):
        """
        `Optional[T]` emits `anyOf: [T, null]`; clients collapse that union
        to an untyped value and then guess at the argument shape.
        """
        assert "anyOf" not in json.dumps(await _schema(components))

    async def test_no_null_type_anywhere(self, components):
        assert '"null"' not in json.dumps(await _schema(components))

    async def test_every_property_has_a_description(self, components):
        schema = await _schema(components)

        missing = [
            name
            for name, spec in schema["properties"].items()
            if not spec.get("description")
        ]

        assert missing == []

    async def test_keywords_is_a_typed_string_array(self, components):
        keywords = (await _schema(components))["properties"]["keywords"]

        assert keywords["type"] == "array"
        assert keywords["items"] == {"type": "string"}

    async def test_optional_params_default_to_empty_not_null(self, components):
        properties = (await _schema(components))["properties"]

        assert properties["alternate_phrasing"]["default"] == ""
        assert properties["keywords"]["default"] == []

    async def test_only_query_is_required(self, components):
        assert (await _schema(components))["required"] == ["query"]

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
    async def test_query_only_call_succeeds(
        self, tool, authenticated_ctx, enrichment_service
    ):
        await tool.run({"query": "user projects"}, context=authenticated_ctx, convert_result=False)

        kwargs = enrichment_service.enrich_context.await_args.kwargs
        assert kwargs["keywords"] == []
        assert kwargs["search_phrase_1"] == "user projects"

    async def test_explicit_nulls_are_accepted(
        self, tool, authenticated_ctx, enrichment_service
    ):
        """
        A client that collapses the optional params sends explicit nulls.
        This is the exact call that used to fail validation.
        """
        await tool.run(
            {"query": "q", "alternate_phrasing": None, "keywords": None},
            context=authenticated_ctx,
            convert_result=False,
        )

        kwargs = enrichment_service.enrich_context.await_args.kwargs
        assert kwargs["keywords"] == []

    async def test_keywords_as_delimited_string_is_accepted(
        self, tool, authenticated_ctx, enrichment_service
    ):
        await tool.run(
            {"query": "q", "keywords": "mcp, schema"},
            context=authenticated_ctx,
            convert_result=False,
        )

        assert enrichment_service.enrich_context.await_args.kwargs["keywords"] == [
            "mcp",
            "schema",
        ]

    async def test_keywords_as_json_string_is_accepted(
        self, tool, authenticated_ctx, enrichment_service
    ):
        await tool.run(
            {"query": "q", "keywords": '["MCP", " Schema "]'},
            context=authenticated_ctx,
            convert_result=False,
        )

        assert enrichment_service.enrich_context.await_args.kwargs["keywords"] == [
            "mcp",
            "schema",
        ]

    async def test_excess_keywords_are_truncated_not_rejected(
        self, tool, authenticated_ctx, enrichment_service
    ):
        await tool.run(
            {"query": "q", "keywords": ["a", "b", "c", "d", "e", "f", "g"]},
            context=authenticated_ctx,
            convert_result=False,
        )

        assert enrichment_service.enrich_context.await_args.kwargs["keywords"] == [
            "a",
            "b",
            "c",
            "d",
            "e",
        ]

    async def test_unknown_argument_is_ignored(
        self, tool, authenticated_ctx, enrichment_service
    ):
        await tool.run(
            {"query": "q", "hypothetical_param": 1},
            context=authenticated_ctx,
            convert_result=False,
        )

        enrichment_service.enrich_context.assert_awaited_once()

    async def test_non_english_query_is_accepted_verbatim(
        self, tool, authenticated_ctx, enrichment_service
    ):
        await tool.run(
            {"query": "  какие у меня проекты  "},
            context=authenticated_ctx,
            convert_result=False,
        )

        kwargs = enrichment_service.enrich_context.await_args.kwargs
        assert kwargs["search_phrase_1"] == "какие у меня проекты"

    async def test_blank_query_with_keywords_still_searches(
        self, tool, authenticated_ctx, enrichment_service
    ):
        await tool.run(
            {"query": "   ", "keywords": ["mcp"]},
            context=authenticated_ctx,
            convert_result=False,
        )

        enrichment_service.enrich_context.assert_awaited_once()


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------


class TestErrorContract:
    async def test_zero_results_is_a_success_not_an_error(
        self, tool, authenticated_ctx
    ):
        result = await tool.run(
            {"query": "q"}, context=authenticated_ctx, convert_result=False
        )

        assert result == "No records matched."

    async def test_entirely_blank_input_is_the_only_rejected_call(
        self, tool, authenticated_ctx, enrichment_service
    ):
        with pytest.raises(ToolError):
            await tool.run(
                {"query": "  ", "alternate_phrasing": "", "keywords": []},
                context=authenticated_ctx,
                convert_result=False,
            )

        enrichment_service.enrich_context.assert_not_awaited()

    async def test_missing_authenticated_user_raises(self, tool, enrichment_service):
        ctx = MagicMock()
        ctx.request_context.request = None

        with pytest.raises(ToolError):
            await tool.run({"query": "q"}, context=ctx, convert_result=False)

        enrichment_service.enrich_context.assert_not_awaited()

    async def test_wrong_access_token_type_raises(self, tool, enrichment_service):
        ctx = MagicMock()
        ctx.request_context.request.user.access_token = object()

        with pytest.raises(ToolError):
            await tool.run({"query": "q"}, context=ctx, convert_result=False)

        enrichment_service.enrich_context.assert_not_awaited()


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------


class TestResultRendering:
    async def test_facts_are_grouped_by_domain(
        self, tool, authenticated_ctx, enrichment_service
    ):
        enrichment_service.enrich_context.return_value = EnrichedContext(
            facts=[
                EnrichedFact(
                    fact_id="1",
                    content="Lives in Valencia",
                    source="phrase1_text",
                    relevance_score=0.9,
                    domain="biography",
                ),
                EnrichedFact(
                    fact_id="2",
                    content="Prefers direct feedback",
                    source="keyword_tags",
                    domain="agent_directive",
                ),
            ],
            total_sources=2,
            dedup_count=0,
            biographical_dedup_count=0,
        )

        result = await tool.run(
            {"query": "q"}, context=authenticated_ctx, convert_result=False
        )

        assert "## agent_directive" in result
        assert "## biography" in result
        assert "- Lives in Valencia (score=0.900)" in result
        assert "- Prefers direct feedback" in result
