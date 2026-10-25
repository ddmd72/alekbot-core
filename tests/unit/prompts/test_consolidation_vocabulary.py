"""H4 vocabulary table for Consolidation (TEST_COVERAGE_SYSTEMIC_RFC §3 H4, P8).

Production renders `prompts_snapshot/profiles/consolidation.yaml` (blueprint `consolidation_agent_v1`). Each token names the
fact vocabulary in prose; this file declares, per (token, field), the enum class or tool schema the prose must agree with.
Out of scope: the unrendered `COGNITIVE_PROCESS_CONSOLIDATION` and the universal profile; USER/ACCOUNT overrides (not mirrored)."""
import pytest

from src.domain.entities import ContextPriority, FactDomain, FactState, TemporalClass
from src.adapters.fact_management_adapter import FactManagementAdapter
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.search import EnrichedContext, EnrichedFact
from tests.support import prompt_vocabulary as pv

PROFILE = pv.load_profile("consolidation")
BLUEPRINT = pv.load_blueprint(PROFILE["blueprint_id"])
TOKENS = pv.load_tokens(PROFILE)

TAXONOMY = "CONSOLIDATION_TAXONOMY"
TOOLS = "CONSOLIDATION_TOOLS"

# (token, field, enum, mode, allowed_other)
#   block:<name> -> the block's keys are exactly the enum's member names (a foreign value and a missing one both fail)
#   lines        -> ALLCAPS words on lines naming the field are enum members or the row's declared contrast words
#   exact:<re>   -> a closed list: the ALLCAPS words group 1 of the regex captures equal the enum's member names (all of them)
POLICY_LISTS = {   # Taxonomy_Enforcement constraint lines, list text only (stops before "(NOT ...)")
    "domain": r"Domain: MUST be from predefined list \(([^)\n]*)\)",
    "temporal_class": r'Temporal Class: MUST be ([^\n"(]*)',
    "state": r'State: MUST be ([^\n"(]*)',
    "context_priority": r'Context Priority: MUST be ([^\n"(]*)',
}
ENUM_ROWS = [
    (TAXONOMY, "domain", FactDomain, "block:domains", set()),                                  # the definition of each axis
    (TAXONOMY, "temporal_class", TemporalClass, "block:temporal_classes", set()),
    (TAXONOMY, "state", FactState, "block:states", set()),
    (TAXONOMY, "context_priority", ContextPriority, "block:context_priorities", set()),
    (TAXONOMY, "domains", FactDomain, "lines", {"ALL"}),                                       # `domains: [...]` in the conflict rules
    ("CONSOLIDATION_COGNITIVE_PROCESS", "domain", FactDomain, "lines", {"UPDATE"}),            # "time series domain (...) -> UPDATE"
    ("CONSOLIDATION_COGNITIVE_PROCESS", "temporal_class", TemporalClass, "lines", {"TTL"}),    # "Determine TTL (based on Temporal Class)"
    ("CONSOLIDATION_COGNITIVE_PROCESS", "state", FactState, "lines", set()),
    ("CONSOLIDATION_EXAMPLES", "domain", FactDomain, "lines", set()),
    ("CONSOLIDATION_EXAMPLES", "temporal_class", TemporalClass, "lines", set()),
    ("CONSOLIDATION_EXAMPLES", "temporal", TemporalClass, "lines", set()),                     # the "- Temporal: X" classify lines
    ("CONSOLIDATION_EXAMPLES", "state", FactState, "lines", set()),
    ("CONSOLIDATION_EXAMPLES", "context_priority", ContextPriority, "lines", set()),
    ("CONSOLIDATION_OUTPUT_SPEC", "state", FactState, "lines", {"UPDATE"}),                    # "UPDATE state=SUPERSEDED"
    ("CONSOLIDATION_POLICIES", "domain", FactDomain, "lines", {"MUST"}),                       # Taxonomy_Enforcement lists
    ("CONSOLIDATION_POLICIES", "temporal_class", TemporalClass, "lines", {"MUST", "NOT", "ARCHIVAL"}),  # "(NOT ARCHIVAL!)"
    ("CONSOLIDATION_POLICIES", "state", FactState, "lines", {"MUST", "NOT", "ARCHIVAL"}),      # "ARCHIVAL is context_priority, ARCHIVED is state"
    ("CONSOLIDATION_POLICIES", "context_priority", ContextPriority, "lines", {"MUST", "NOT", "ARCHIVED"}),  # "(NOT ARCHIVED!)"
    (TOOLS, "domain", FactDomain, "lines", {"MUST", "REQUIRED", "UUID"}),                      # search `returns:` line names domain
    (TOOLS, "temporal_class", TemporalClass, "lines", {"MUST", "REQUIRED", "UUID"}),           # search `returns:` line names it too
    (TOOLS, "state", FactState, "lines", {"MUST", "REQUIRED", "UUID"}),
    (TOOLS, "context_priority", ContextPriority, "lines", {"MUST"}),
    ("CONSOLIDATION_POLICIES", "domain", FactDomain, "exact:" + POLICY_LISTS["domain"], set()),
    ("CONSOLIDATION_POLICIES", "temporal_class", TemporalClass, "exact:" + POLICY_LISTS["temporal_class"], set()),
    ("CONSOLIDATION_POLICIES", "state", FactState, "exact:" + POLICY_LISTS["state"], set()),
    ("CONSOLIDATION_POLICIES", "context_priority", ContextPriority, "exact:" + POLICY_LISTS["context_priority"], set()),
    (TOOLS, "temporal_class", TemporalClass, r'exact:\btemporal_class: "TemporalClass \(([^)\n]*)\)', set()),   # create_fact
    (TOOLS, "context_priority", ContextPriority, r'exact:\bcontext_priority: "ContextPriority \(([^)\n]*)\)', set()),
]

# Tools the code declares but no profile token names: the model reads them from the schema's own description.
TOOLS_NOT_IN_PROMPT = {"count_words"}   # schema: "Call this BEFORE create_fact or update_fact ..."

CODE_TOOLS = {t["name"]: t["parameters"] for t in ConsolidationAgent._get_tool_declarations(None)}
PROMPT_TOOLS = pv.groovy_tools(TOKENS[TOOLS][1])


def _members(enum):
    return {m.name for m in enum}


def test_every_profile_token_exists_and_the_blueprint_orders_each_class_once():
    # TOKENS loading already raised if a listed token has no file
    classes = [cls for cls, _ in TOKENS.values()]
    assert len(classes) == len(set(classes)), f"two profile tokens share a class: {classes}"
    assert sorted(classes) == sorted(BLUEPRINT["class_order"]), (
        f"blueprint orders {BLUEPRINT['class_order']}, profile tokens have {classes}")


@pytest.mark.parametrize("token,field,enum,mode,other", ENUM_ROWS, ids=[f"{r[0]}-{r[1]}-{r[3][:12]}" for r in ENUM_ROWS])
def test_token_vocabulary_matches_the_enum(token, field, enum, mode, other):
    body = TOKENS[token][1]
    if mode.startswith("block:"):
        keys = pv.block_keys(body, mode.split(":", 1)[1])
        assert set(keys) == _members(enum) and len(keys) == len(set(keys)), (
            f"{token} {mode}: prompt has {sorted(keys)}, {enum.__name__} has {sorted(_members(enum))}")
    elif mode.startswith("exact:"):
        lists = pv.pattern_words(body, mode[len("exact:"):])
        assert lists, f"{token} {field}: the list line is gone ({mode})"
        for words in lists:
            assert words == _members(enum), f"{token} {field}: list has {sorted(words)}, {enum.__name__} has {sorted(_members(enum))}"
    else:
        foreign = pv.field_line_words(body, field) - _members(enum) - other
        assert not foreign, f"{token} names {sorted(foreign)} on lines about {field}; {enum.__name__} has {sorted(_members(enum))}"


@pytest.mark.parametrize("tool", sorted(PROMPT_TOOLS))
def test_prompt_tool_matches_the_code_schema(tool):
    assert tool in CODE_TOOLS, f"{TOOLS} declares @tool {tool}, the code does not"
    spec, prompt = CODE_TOOLS[tool], PROMPT_TOOLS[tool]
    props = spec["properties"]
    assert set(prompt["signature"]) <= set(props), f"{tool} signature has {set(prompt['signature']) - set(props)} the code lacks"
    assert prompt["params"] <= set(props), f"{tool} parameters has {prompt['params'] - set(props)} the code lacks"
    assert set(spec["required"]) <= prompt["params"], f"{tool}: code requires {set(spec['required']) - prompt['params']}, the prompt omits it"
    for key, sub in prompt["nested"].items():
        code_sub = props[key]
        assert sub <= set(code_sub.get("properties", {})), f"{tool}.{key} has {sub - set(code_sub.get('properties', {}))} the code lacks"
        assert set(code_sub.get("required", [])) <= sub, f"{tool}.{key}: code requires {set(code_sub.get('required', [])) - sub}, the prompt omits it"


async def test_search_results_carry_the_keys_the_prompt_says_they_do():
    """`search_existing_facts` `returns:` names keys; each must be a key of a real adapter result."""
    class Stub:
        async def enrich_context(self, **_):
            return EnrichedContext(facts=[EnrichedFact(fact_id="f", content="c", source="keyword")], total_sources=1,
                                   dedup_count=0, biographical_dedup_count=0)
    adapter = FactManagementAdapter(None, None, None, Stub())   # the other ports are not touched by a search
    # Reviewer edit (facts S3, T4a; plan §6 E): the search takes the owner's zone, a required keyword from T4a on.
    [result] = await adapter.search_existing_facts(["k"], "q", timezone="UTC")
    named = pv.returns_keys(TOKENS[TOOLS][1])["search_existing_facts"]
    assert named and named <= set(result), f"returns: names {sorted(named - set(result))}; the result has {sorted(result)}"


def test_every_code_tool_is_named_by_a_profile_token():
    named = {name for _, body in TOKENS.values() for name in CODE_TOOLS if name in body}
    assert set(CODE_TOOLS) - named == TOOLS_NOT_IN_PROMPT


def test_edited_tokens_round_trip_through_the_serializer():
    for token_id in (TAXONOMY, TOOLS, "CONSOLIDATION_POLICIES"):
        text = pv.token_path(token_id).read_text(encoding="utf-8")
        assert pv.ser.token_to_file(pv.ser.token_from_file(text)) == text
