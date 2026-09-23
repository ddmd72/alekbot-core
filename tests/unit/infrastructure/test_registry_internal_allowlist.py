from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, QUICK_RESPONSE, SMART_RESPONSE
from src.infrastructure.agent_registry import AgentDescriptor, AgentRegistry, ExecutionMode


def _reg(*descs):
    reg = AgentRegistry()
    for d in descs:
        reg.register(d)
    return reg


_PUB = AgentDescriptor(agent_id="pub", capabilities={"pub_i": ExecutionMode.SYNC})
_PRIV = AgentDescriptor(agent_id="priv", capabilities={"priv_i": ExecutionMode.SYNC}, internal=True)


def test_explicit_allowlist_can_name_an_internal_intent():
    caller = AgentDescriptor(agent_id="c", allowed_intents=frozenset({"pub_i", "priv_i"}))
    assert {i["name"] for i in _reg(_PUB, _PRIV).get_available_intents_for(caller)} == {"pub_i", "priv_i"}


def test_no_allowlist_still_never_sees_internal():
    caller = AgentDescriptor(agent_id="c", allowed_intents=None)
    assert {i["name"] for i in _reg(_PUB, _PRIV).get_available_intents_for(caller)} == {"pub_i"}


def test_default_listing_still_hides_internal():
    assert {i["name"] for i in _reg(_PUB, _PRIV).get_available_intents()} == {"pub_i"}


def test_no_existing_allowlist_changes_its_tool_list():
    reg = _reg(*ALL_DESCRIPTORS)
    for d in ALL_DESCRIPTORS:
        if d.allowed_intents is None:
            continue
        visible = {i["name"] for i in reg.get_available_intents_for(d)}
        internal = {i for x in ALL_DESCRIPTORS if x.internal for i in x.capabilities}
        if d.agent_id != "lelik_agent":
            assert not (visible & internal), f"{d.agent_id} now sees internal intents {visible & internal}"
    for orch in (QUICK_RESPONSE, SMART_RESPONSE):
        assert "maps_query" not in {i["name"] for i in reg.get_available_intents_for(orch)}
