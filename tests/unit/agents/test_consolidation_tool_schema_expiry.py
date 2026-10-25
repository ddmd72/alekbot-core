"""The consolidator's tool schema offers `expiry` and only the states the model may set (Facts S3, T5, plan §3.3).

`_get_tool_declarations` reads no instance state, so the tests call it with `None` as the other schema tests do.
"""
import copy

import pytest

from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.fact_expiry import parse_expiry


def declarations():
    return {d["name"]: d["parameters"] for d in ConsolidationAgent._get_tool_declarations(None)}  # type: ignore[arg-type]


def create_attrs():
    return declarations()["create_fact"]["properties"]["fact_attributes"]


def merge_attrs():
    return declarations()["merge_facts"]["properties"]["fact_attributes"]


def update_props():
    return declarations()["update_fact"]["properties"]["updates"]["properties"]


def walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def test_no_tool_offers_ttl_days():
    assert "ttl_days" not in set(walk(list(declarations().values())))


@pytest.mark.parametrize("attrs", [create_attrs, merge_attrs])
def test_create_and_merge_require_expiry_beside_the_old_required_keys(attrs):
    assert attrs()["required"] == ["domain", "temporal_class", "context_priority", "tags", "expiry"]


def test_expiry_is_one_flat_described_object_with_only_kind_required():
    expiry = create_attrs()["properties"]["expiry"]
    assert expiry["type"] == "object" and expiry["description"]
    assert expiry["required"] == ["kind"]
    assert expiry["properties"]["kind"]["enum"] == ["date", "horizon", "none"]
    assert set(expiry["properties"]) == {"kind", "date", "days"}
    assert all(spec["description"] for spec in expiry["properties"].values())
    assert expiry["properties"]["days"]["type"] == "integer"
    assert "anyOf" not in expiry and "oneOf" not in expiry


@pytest.mark.parametrize("sent", [
    {"kind": "date", "date": "2026-12-31"}, {"kind": "horizon", "days": 14}, {"kind": "none"},
])
def test_every_shape_the_schema_describes_is_one_the_parser_accepts(sent):
    props = create_attrs()["properties"]["expiry"]["properties"]
    assert set(sent) <= set(props)
    assert parse_expiry(sent)


def test_expiry_and_derived_from_are_described_wherever_they_appear():
    assert create_attrs()["properties"]["derived_from"]["description"]
    assert create_attrs()["properties"]["expiry"]["description"]
    assert merge_attrs()["properties"]["expiry"]["description"]
    assert update_props()["expiry"]["description"]


def test_derived_from_is_a_list_of_ids_on_create_only():
    derived = create_attrs()["properties"]["derived_from"]
    assert derived["type"] == "array" and derived["items"] == {"type": "string"}
    assert "derived_from" not in merge_attrs()["properties"]
    assert "cluster or rulebook review" in derived["description"]


def test_merge_offers_the_same_properties_as_create_except_derived_from():
    create = copy.deepcopy(create_attrs()["properties"])
    del create["derived_from"]
    assert merge_attrs()["properties"] == create


@pytest.mark.parametrize("attrs", [create_attrs, merge_attrs])
def test_create_and_merge_state_offers_neither_stale_nor_superseded(attrs):
    state = attrs()["properties"]["state"]["description"]
    assert {part.strip() for part in state.split("/")} == {"CURRENT", "ARCHIVED", "INVALIDATED"}
    note = attrs()["description"]
    assert "stale is set by the system" in note and "superseded only through update_fact" in note


def test_update_fact_offers_expiry_and_names_what_needs_it():
    updates = declarations()["update_fact"]["properties"]["updates"]
    description = updates["properties"]["expiry"]["description"]
    assert "text, tags, metadata or temporal class" in description
    assert "expiry" in updates["description"]
    assert "stale is set by the system" in updates["description"]
    assert "superseded_by" in updates["properties"]


def test_reported_date_is_described_as_the_owners_message_time_with_its_offset():
    for description in (create_attrs()["properties"]["reported_date"]["description"],
                        merge_attrs()["properties"]["reported_date"]["description"],
                        update_props()["reported_date"]["description"]):
        assert "owner's message" in description and "offset" in description and "2026-10-22T12:00+02:00" in description


def test_every_call_returns_its_own_schema_objects():
    first, second = declarations(), declarations()
    first["create_fact"]["properties"]["fact_attributes"]["properties"]["expiry"]["properties"]["kind"]["enum"].append("x")
    first["update_fact"]["properties"]["updates"]["properties"]["expiry"]["required"].append("y")
    assert second["create_fact"]["properties"]["fact_attributes"]["properties"]["expiry"]["properties"]["kind"]["enum"] == [
        "date", "horizon", "none"]
    assert declarations()["merge_facts"]["properties"]["fact_attributes"]["properties"]["expiry"]["required"] == ["kind"]
    assert declarations()["update_fact"]["properties"]["updates"]["properties"]["expiry"]["required"] == ["kind"]
