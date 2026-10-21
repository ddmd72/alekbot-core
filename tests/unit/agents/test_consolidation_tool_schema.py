"""update_fact tool declaration advertises metadata."""

from src.agents.consolidation_agent import ConsolidationAgent


def test_update_fact_updates_accept_metadata_object():
    # _get_tool_declarations does not read instance state
    decls = ConsolidationAgent._get_tool_declarations(None)  # type: ignore[arg-type]
    update_fact = next(d for d in decls if d["name"] == "update_fact")
    props = update_fact["parameters"]["properties"]["updates"]["properties"]
    assert props["metadata"]["type"] == "object"
    assert "metadata" in update_fact["parameters"]["properties"]["updates"]["description"]
