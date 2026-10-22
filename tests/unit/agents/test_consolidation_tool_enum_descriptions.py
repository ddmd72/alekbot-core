"""create_fact's enum descriptions name exactly the members of the domain enums."""

from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.entities import ContextPriority, FactDomain, FactState, TemporalClass


def _attribute_descriptions() -> dict:
    # _get_tool_declarations does not read instance state
    decls = ConsolidationAgent._get_tool_declarations(None)  # type: ignore[arg-type]
    create_fact = next(d for d in decls if d["name"] == "create_fact")
    props = create_fact["parameters"]["properties"]["fact_attributes"]["properties"]
    return {name: spec["description"] for name, spec in props.items()}


def _named_members(description: str) -> set:
    return {part.strip() for part in description.split("/")}


def test_state_description_names_every_fact_state():
    described = _named_members(_attribute_descriptions()["state"])
    assert described == {m.name for m in FactState}


def test_temporal_class_description_names_every_member():
    described = _named_members(_attribute_descriptions()["temporal_class"])
    assert described == {m.name for m in TemporalClass}


def test_context_priority_description_names_every_member():
    described = _named_members(_attribute_descriptions()["context_priority"])
    assert described == {m.name for m in ContextPriority}


def test_domain_description_names_every_fact_domain():
    described = _named_members(_attribute_descriptions()["domain"])
    assert described == {m.name for m in FactDomain}
