"""The review pass's tool declarations (Facts S4 plan §3.5, T6b): three tools, every property described, no `reported_date`,
the horizon counted from now, and no constraint advertised that the review's parsers would turn into a refusal."""

from typing import get_args

import pytest

from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.fact_review_question import TEXT_KEYS, QuestionFallback, QuestionKind

_CONSTRAINTS = ("enum", "minimum", "maximum", "exclusiveMinimum", "pattern", "minItems", "maxItems", "additionalProperties",
                "minLength", "maxLength", "format")


def review_tools():
    return {tool["name"]: tool for tool in ConsolidationAgent._get_tool_declarations(None, "review")}


def properties_of(schema, path=""):
    """Every (path, property schema) under `schema`, nested objects and array items included."""
    for name, prop in (schema.get("properties") or {}).items():
        here = f"{path}.{name}" if path else name
        yield here, prop
        yield from properties_of(prop, here)
        if isinstance(prop.get("items"), dict):
            yield from properties_of(prop["items"], f"{here}[]")


def test_the_review_offers_exactly_its_three_tools():
    assert sorted(review_tools()) == ["ask_owner", "count_words", "update_fact"]


def test_the_consolidation_passes_keep_their_tools_and_an_unknown_kind_is_refused():
    names = [t["name"] for t in ConsolidationAgent._get_tool_declarations(None)]
    assert names == ["search_existing_facts", "create_fact", "update_fact", "count_words", "merge_facts"]
    assert ConsolidationAgent._get_tool_declarations(None, "consolidation") == ConsolidationAgent._get_tool_declarations(None)
    with pytest.raises(ValueError):
        ConsolidationAgent._get_tool_declarations(None, "janitor")    # type: ignore[arg-type]


@pytest.mark.parametrize("tool", ["update_fact", "ask_owner", "count_words"])
def test_every_property_of_a_review_tool_is_described(tool):
    schema = review_tools()[tool]
    assert schema["description"].strip()
    props = list(properties_of(schema["parameters"]))
    assert props
    missing = [path for path, prop in props if not str(prop.get("description", "")).strip()]
    assert missing == []


@pytest.mark.parametrize("tool", ["update_fact", "ask_owner"])
def test_no_review_tool_advertises_a_constraint_its_parser_would_refuse(tool):
    found = [(path, key) for path, prop in properties_of(review_tools()[tool]["parameters"]) for key in _CONSTRAINTS if key in prop]
    assert found == []
    assert "additionalProperties" not in review_tools()[tool]["parameters"]


def test_the_review_update_takes_basis_and_history_and_no_reported_date():
    params = review_tools()["update_fact"]["parameters"]
    # P2 revised (P2-1, P2-2): as_history (a boolean) and payment (the two invoice texts) join the review's update
    assert set(params["properties"]) == {"fact_id", "updates", "basis", "history", "as_history", "payment"}
    assert params["properties"]["as_history"]["type"] == "boolean"
    assert set(params["properties"]["payment"]["properties"]) == {"paid", "not_paid"}
    assert set(params["properties"]["updates"]["properties"]) == {
        "content", "tags", "metadata", "temporal_class", "state", "superseded_by", "expiry",
    }
    assert all("reported_date" not in path for path, _ in properties_of(params))
    assert params["required"] == ["fact_id", "updates"]


def test_the_review_states_cover_revive_and_every_closing_state():
    state = review_tools()["update_fact"]["parameters"]["properties"]["updates"]["properties"]["state"]["description"]
    for value in ("CURRENT", "ARCHIVED", "INVALIDATED", "SUPERSEDED"):
        assert value in state
    assert "STALE" not in state


def test_the_basis_names_its_three_kinds_and_their_fields():
    basis = review_tools()["update_fact"]["parameters"]["properties"]["basis"]
    assert set(basis["properties"]) == {"kind", "evidence_fact_id", "question_id", "quote", "date"}
    assert basis["required"] == ["kind"]
    for kind in ("evidence", "reply", "date_passed"):
        assert kind in basis["properties"]["kind"]["description"] and kind in basis["description"]


def test_the_expiry_says_a_horizon_counts_from_now_and_n_is_at_least_two():
    expiry = review_tools()["update_fact"]["parameters"]["properties"]["updates"]["properties"]["expiry"]
    assert (
        "a horizon counts from now: `{kind: horizon, days: N}` means review again in N days (N ≥ 2)"
        in expiry["description"]
    )
    assert "N ≥ 2" in expiry["properties"]["days"]["description"]
    assert "said it" not in expiry["description"] and "said it" not in expiry["properties"]["days"]["description"]


def test_ask_owner_takes_a_fact_and_its_fallback_and_no_question_text():
    params = review_tools()["ask_owner"]["parameters"]
    # P2 revised (P2-3, C8): the ask gives its kind, the texts code writes and its folds; fallback_date is retired
    assert set(params["properties"]) == {"fact_id", "question", "texts", "fallback", "folds", "noise"}
    assert params["required"] == ["fact_id", "fallback"]
    for fallback in get_args(QuestionFallback):
        assert fallback in params["properties"]["fallback"]["description"]
    for kind in get_args(QuestionKind):
        assert kind in params["properties"]["question"]["description"]
    assert set(params["properties"]["texts"]["properties"]) == {key for keys in TEXT_KEYS.values() for key in keys}
    assert params["properties"]["folds"]["items"]["type"] == "string"
    assert params["properties"]["noise"]["type"] == "boolean"


def test_each_call_gets_fresh_copies():
    first, second = review_tools(), review_tools()
    first["update_fact"]["parameters"]["properties"]["updates"]["properties"]["expiry"]["description"] = "changed"
    first["update_fact"]["parameters"]["properties"]["basis"]["description"] = "changed"
    assert second["update_fact"]["parameters"]["properties"]["basis"]["description"] != "changed"
    assert review_tools()["update_fact"]["parameters"]["properties"]["updates"]["properties"]["expiry"]["description"] != "changed"


def test_the_review_count_words_names_only_the_review_tools():
    text = review_tools()["count_words"]["description"]
    assert "create_fact" not in text and "update_fact" in text
    consolidation = {t["name"]: t for t in ConsolidationAgent._get_tool_declarations(None)}["count_words"]["description"]
    assert consolidation.startswith("Count words in a text string. Call this BEFORE create_fact or update_fact")
