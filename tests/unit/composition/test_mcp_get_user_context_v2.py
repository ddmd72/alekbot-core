"""The claude.ai / voice connector tool `get_user_context` on `queries` (fact profile RFC §5.4, §6 row 10).

The tool takes one to four English phrases and answers through the user's memory search agent. The agent here is a
fake that records what it was asked; its own search is tested with the agent.
"""

import json
from typing import List, Optional

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from unittest.mock import MagicMock

from src.composition.mcp_sdk_oauth_provider import AlekAccessToken
from src.composition.mcp_setup import build_mcp_components
from src.domain.entities import FactEntity, FactType
from src.domain.memory_search_texts import QUERIES_FIELD_TEXT
from src.composition.mcp_setup import FACTS_TOOL
from src.domain.voice_mcp_token import McpTokenKind
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.mcp_gate_fakes import allowing_repos

_RESOURCE_URI = "https://mcp.example.test/mcp"
_NO_QUERIES = "No search phrases supplied. Pass one to four English phrases as queries, most important first."


def _fact(content: str, *, domain: Optional[str] = None, primary: Optional[str] = None,
          points: Optional[list] = None, account: str = "acc-1") -> FactEntity:
    return FactEntity(
        id=f"f-{abs(hash(content)) % 10_000}", account_id=account, created_by_user_id="user-1", lineage_id="l",
        text=content, type=FactType.EVENT, domain=domain, primary_domain=primary, time_points=points,
    )


class FakeMemoryAgent:
    """What the connector needs of a FactsMemoryAgent: a tenant and `search_facts`."""

    def __init__(self, facts: List[FactEntity], account_id: str = "acc-1"):
        self.facts = facts
        self.account_id = account_id
        self.calls: list = []

    async def search_facts(self, queries, *, path):
        self.calls.append((list(queries), path))
        return self.facts


class Resolver:
    """The `memory_search` callable: records the user it was asked for."""

    def __init__(self, agent):
        self.agent = agent
        self.users: list = []

    async def __call__(self, user_id):
        self.users.append(user_id)
        return self.agent


def _components(resolver):
    return build_mcp_components(
        db_client=MagicMock(), env_config=MagicMock(), auth_config=_auth_config(), memory_search=resolver,
        cipher=AesGcmTestCipher(), digest_key=bytes(range(32)),
        user_repo=allowing_repos("u1")[0], whitelist_repo=allowing_repos("u1")[1],
    )


def _auth_config() -> MagicMock:
    cfg = MagicMock()
    cfg.mcp_resource_uri = _RESOURCE_URI
    cfg.oauth_session_secret = "x" * 48
    cfg.mcp_access_token_ttl = 3600
    cfg.mcp_refresh_token_ttl = 2592000
    cfg.mcp_auth_code_ttl = 600
    cfg.mcp_consent_request_ttl = 600
    return cfg


def _ctx(kind=McpTokenKind.OAUTH, tools=None, user="user-1", account="acc-1"):
    token = AlekAccessToken(
        token="jwt", client_id="mcp-test", scopes=["user_context"], expires_at=4102444800, resource=_RESOURCE_URI,
        user_id=user, account_id=account, kind=kind, tools=tools,
    )
    ctx = MagicMock()
    ctx.request_context.request.user.access_token = token
    return ctx


def _setup(facts=(), agent_account="acc-1"):
    agent = FakeMemoryAgent(list(facts), account_id=agent_account)
    resolver = Resolver(agent)
    components = _components(resolver)
    return components.fastmcp._tool_manager.get_tool(FACTS_TOOL), agent, resolver, components


class TestSchema:
    async def test_exactly_one_parameter_queries_with_the_shared_text(self):
        _, _, _, components = _setup()
        tool = next(t for t in await components.fastmcp.list_tools() if t.name == FACTS_TOOL)
        props = tool.inputSchema["properties"]
        assert list(props) == ["queries"]
        assert props["queries"]["type"] == "array"
        assert props["queries"]["items"] == {"type": "string"}
        assert props["queries"]["description"] == QUERIES_FIELD_TEXT
        assert "anyOf" not in json.dumps(tool.inputSchema)


class TestInput:
    async def test_a_json_encoded_array_is_accepted(self):
        tool, agent, _, _ = _setup()
        await tool.run({"queries": json.dumps(["Clara Moreira passport expiry", "Alex passport"])}, context=_ctx())
        assert agent.calls == [(["Clara Moreira passport expiry", "Alex passport"], "connector")]

    async def test_a_single_string_is_accepted(self):
        tool, agent, _, _ = _setup()
        await tool.run({"queries": "Alex owns a 2015 Skoda Fabia"}, context=_ctx())
        assert agent.calls == [(["Alex owns a 2015 Skoda Fabia"], "connector")]

    async def test_five_phrases_search_four_and_log_the_dropped_one(self, monkeypatch):
        tool, agent, _, _ = _setup()
        lines: list = []
        monkeypatch.setattr("src.composition.mcp_setup.logger.info", lambda m, *a, **k: lines.append(m))
        await tool.run({"queries": ["a1", "b2", "c3", "d4", "e5"]}, context=_ctx())
        assert agent.calls[0][0] == ["a1", "b2", "c3", "d4"]
        search_line = next(m for m in lines if "queries=" in m)
        assert "dropped=1" in search_line and "'a1'" in search_line

    async def test_nothing_usable_is_the_one_rejection_and_nothing_is_searched(self):
        tool, agent, resolver, _ = _setup()
        for empty in ([], ["", "  "], None, ""):
            with pytest.raises(ToolError) as err:
                await tool.run({"queries": empty}, context=_ctx())
            assert _NO_QUERIES in str(err.value)
        assert agent.calls == [] and resolver.users == []


class TestAgentCall:
    async def test_the_agent_is_asked_for_the_tokens_user_with_the_phrases_in_order(self):
        tool, agent, resolver, _ = _setup()
        await tool.run({"queries": ["car service date", "Alex car"]}, context=_ctx(user="user-7"))
        assert resolver.users == ["user-7"]
        assert agent.calls == [(["car service date", "Alex car"], "connector")]

    async def test_another_tenants_agent_is_refused_and_logged(self, monkeypatch):
        tool, agent, _, _ = _setup(facts=[_fact("secret", account="acc-2")], agent_account="acc-2")
        warnings: list = []
        monkeypatch.setattr("src.composition.mcp_setup.logger.warning", lambda m, *a, **k: warnings.append(m))
        with pytest.raises(ToolError) as err:
            await tool.run({"queries": ["car"]}, context=_ctx(account="acc-1"))
        assert "not available for this account" in str(err.value)
        assert agent.calls == []
        assert any("another account" in w for w in warnings)

    async def test_no_agent_for_the_user_is_refused(self):
        tool, _, resolver, _ = _setup()
        resolver.agent = None
        with pytest.raises(ToolError) as err:
            await tool.run({"queries": ["car"]}, context=_ctx())
        assert "Memory search is not available for this account." in str(err.value)


class TestResult:
    async def test_facts_group_under_the_english_label_of_the_primary_domain_with_when(self):
        facts = [
            _fact("Clara Moreira's passport is valid until 2026-12-09", primary="state_law",
                  points=[{"from": "2026-12-09"}]),
            _fact("Alex owns a 2015 Skoda Fabia", primary="transport"),
            _fact("Car service on 2026-11-05", primary="transport", points=[{"from": "2026-11-05"}]),
        ]
        tool, _, _, _ = _setup(facts)
        out = await tool.run({"queries": ["passport", "car"]}, context=_ctx())
        assert out == (
            "## State & law\n"
            "- Clara Moreira's passport is valid until 2026-12-09 (when: 2026-12-09)\n"
            "\n"
            "## Transport\n"
            "- Alex owns a 2015 Skoda Fabia\n"
            "- Car service on 2026-11-05 (when: 2026-11-05)"
        )

    async def test_a_fact_without_a_primary_domain_falls_back_to_its_old_domain_then_general(self):
        facts = [_fact("Alex owns a 2015 Skoda Fabia", domain="possession"), _fact("Car service on 2026-11-05")]
        tool, _, _, _ = _setup(facts)
        out = await tool.run({"queries": ["car"]}, context=_ctx())
        assert "## possession\n- Alex owns a 2015 Skoda Fabia" in out
        assert "## general\n- Car service on 2026-11-05" in out
        assert "score" not in out

    async def test_an_empty_result_says_no_records_matched(self):
        tool, _, _, _ = _setup([])
        assert await tool.run({"queries": ["car"]}, context=_ctx()) == "No records matched."
