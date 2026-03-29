"""
Unit tests for AgentRegistry.

Coverage:
  register()
    - registers descriptor and maps intents
    - overwrites existing registration for same agent_id
    - logs warning when intent claimed by another agent

  get_agent_for_intent()
    - known intent → returns descriptor
    - unknown intent → returns None

  get_execution_mode()
    - known intent → returns ExecutionMode
    - unknown intent → returns None

  get_available_intents()
    - returns non-internal intents with descriptions
    - uses capability_descriptions when present
    - falls back to agent description when no per-intent description
    - excludes internal agents
    - includes context_schema when present
    - empty registry → empty list

  get_available_intents_for()
    - allowed_intents=None → returns all non-internal
    - allowed_intents frozenset → filters to subset
    - allowed_intents includes intent not in registry → silently absent

  list_agents()
    - returns all registered descriptors
"""
import pytest

from src.infrastructure.agent_registry import AgentDescriptor, AgentRegistry, ExecutionMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _desc(
    agent_id="test_agent",
    *,
    intents=("do_thing",),
    descriptions=None,
    internal=False,
    description="fallback desc",
    context_schemas=None,
    allowed_intents=None,
    intent_remap=None,
) -> AgentDescriptor:
    capabilities = {i: ExecutionMode.SYNC for i in intents}
    return AgentDescriptor(
        agent_id=agent_id,
        agent_type=agent_id,
        capabilities=capabilities,
        capability_descriptions=descriptions or {},
        context_schemas=context_schemas or {},
        internal=internal,
        description=description,
        allowed_intents=allowed_intents,
        intent_remap=intent_remap or {},
    )


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

class TestRegister:

    def test_registers_descriptor(self):
        reg = AgentRegistry()
        d = _desc("agent_a", intents=("intent_x",))
        reg.register(d)
        assert reg.get_agent_for_intent("intent_x") is d

    def test_overwrites_same_agent_id(self):
        reg = AgentRegistry()
        d1 = _desc("agent_a", intents=("intent_x",), description="v1")
        d2 = _desc("agent_a", intents=("intent_x",), description="v2")
        reg.register(d1)
        reg.register(d2)
        assert reg.get_agent_for_intent("intent_x").description == "v2"

    def test_warns_when_intent_claimed_by_another_agent(self, caplog):
        import logging
        reg = AgentRegistry()
        reg.register(_desc("agent_a", intents=("shared_intent",)))
        with caplog.at_level(logging.WARNING):
            reg.register(_desc("agent_b", intents=("shared_intent",)))
        assert any("shared_intent" in r.message for r in caplog.records)

    def test_multiple_intents_all_mapped(self):
        reg = AgentRegistry()
        d = _desc("agent_a", intents=("i1", "i2", "i3"))
        reg.register(d)
        assert reg.get_agent_for_intent("i1") is d
        assert reg.get_agent_for_intent("i2") is d
        assert reg.get_agent_for_intent("i3") is d


# ---------------------------------------------------------------------------
# get_agent_for_intent()
# ---------------------------------------------------------------------------

class TestGetAgentForIntent:

    def test_known_intent_returns_descriptor(self):
        reg = AgentRegistry()
        d = _desc("agent_a", intents=("do_thing",))
        reg.register(d)
        assert reg.get_agent_for_intent("do_thing") is d

    def test_unknown_intent_returns_none(self):
        reg = AgentRegistry()
        assert reg.get_agent_for_intent("nonexistent") is None


# ---------------------------------------------------------------------------
# get_execution_mode()
# ---------------------------------------------------------------------------

class TestGetExecutionMode:

    def test_known_intent_returns_mode(self):
        reg = AgentRegistry()
        d = AgentDescriptor(
            agent_id="async_agent",
            capabilities={"heavy_task": ExecutionMode.ASYNC},
        )
        reg.register(d)
        assert reg.get_execution_mode("heavy_task") == ExecutionMode.ASYNC

    def test_unknown_intent_returns_none(self):
        reg = AgentRegistry()
        assert reg.get_execution_mode("ghost_intent") is None


# ---------------------------------------------------------------------------
# get_available_intents()
# ---------------------------------------------------------------------------

class TestGetAvailableIntents:

    def test_empty_registry_returns_empty_list(self):
        assert AgentRegistry().get_available_intents() == []

    def test_non_internal_intent_included(self):
        reg = AgentRegistry()
        reg.register(_desc("agent_a", intents=("search_x",), description="Search X"))
        intents = reg.get_available_intents()
        names = [i["name"] for i in intents]
        assert "search_x" in names

    def test_internal_agent_excluded(self):
        reg = AgentRegistry()
        reg.register(_desc("agent_a", intents=("internal_op",), internal=True))
        assert reg.get_available_intents() == []

    def test_uses_per_intent_description(self):
        reg = AgentRegistry()
        reg.register(_desc(
            "agent_a",
            intents=("do_thing",),
            descriptions={"do_thing": "Per-intent desc"},
            description="Agent-level desc",
        ))
        item = reg.get_available_intents()[0]
        assert item["description"] == "Per-intent desc"

    def test_falls_back_to_agent_description(self):
        reg = AgentRegistry()
        reg.register(_desc("agent_a", intents=("do_thing",), description="Agent fallback"))
        item = reg.get_available_intents()[0]
        assert item["description"] == "Agent fallback"

    def test_context_schema_included_when_present(self):
        reg = AgentRegistry()
        schema = {"do_thing": {"param_a": "Description of param_a"}}
        reg.register(_desc("agent_a", intents=("do_thing",), context_schemas=schema))
        item = reg.get_available_intents()[0]
        assert "context_schema" in item
        assert item["context_schema"] == {"param_a": "Description of param_a"}

    def test_no_context_schema_key_absent(self):
        reg = AgentRegistry()
        reg.register(_desc("agent_a", intents=("do_thing",)))
        item = reg.get_available_intents()[0]
        assert "context_schema" not in item

    def test_mix_of_internal_and_external(self):
        reg = AgentRegistry()
        reg.register(_desc("agent_pub", intents=("pub_intent",), internal=False))
        reg.register(_desc("agent_priv", intents=("priv_intent",), internal=True))
        intents = {i["name"] for i in reg.get_available_intents()}
        assert "pub_intent" in intents
        assert "priv_intent" not in intents


# ---------------------------------------------------------------------------
# get_available_intents_for()
# ---------------------------------------------------------------------------

class TestGetAvailableIntentsFor:

    def test_allowed_intents_none_returns_all_non_internal(self):
        reg = AgentRegistry()
        reg.register(_desc("agent_a", intents=("i1",)))
        reg.register(_desc("agent_b", intents=("i2",)))
        orchestrator = _desc("orch", intents=(), allowed_intents=None)
        result = reg.get_available_intents_for(orchestrator)
        names = {i["name"] for i in result}
        assert {"i1", "i2"} == names

    def test_allowed_intents_frozenset_filters_results(self):
        reg = AgentRegistry()
        reg.register(_desc("agent_a", intents=("i1",)))
        reg.register(_desc("agent_b", intents=("i2",)))
        orchestrator = _desc("orch", intents=(), allowed_intents=frozenset({"i1"}))
        result = reg.get_available_intents_for(orchestrator)
        assert len(result) == 1
        assert result[0]["name"] == "i1"

    def test_allowed_intents_with_unregistered_intent(self):
        reg = AgentRegistry()
        reg.register(_desc("agent_a", intents=("i1",)))
        orchestrator = _desc("orch", intents=(), allowed_intents=frozenset({"i1", "ghost"}))
        result = reg.get_available_intents_for(orchestrator)
        assert len(result) == 1
        assert result[0]["name"] == "i1"


# ---------------------------------------------------------------------------
# list_agents()
# ---------------------------------------------------------------------------

class TestListAgents:

    def test_returns_all_descriptors(self):
        reg = AgentRegistry()
        d1 = _desc("a1")
        d2 = _desc("a2")
        reg.register(d1)
        reg.register(d2)
        agents = reg.list_agents()
        ids = {a.agent_id for a in agents}
        assert ids == {"a1", "a2"}

    def test_empty_registry_returns_empty_list(self):
        assert AgentRegistry().list_agents() == []
