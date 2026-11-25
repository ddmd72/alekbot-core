"""The memory inbox triage prompt (MCP_MEMORY_SUBMISSIONS_RFC §7.2, plan §15.1): profile `consolidation_inbox_triage`,
blueprint `consolidation_inbox_triage_v1`, the taxonomy then the triage token. The token is Explore-stage, so only its
structure is checked: the profile renders through the production assembler, it holds the two tokens, and none of the
fact-writing rules a pass that writes no fact must not read (the rules `Tool_Call_Mandatory` and `Domain_Scope`, the
operations report). The triage tools' texts stay within the description cap a client takes whole."""
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.connector_texts import MAX_TOOL_DESCRIPTION_CHARS
from src.domain.consolidation import INBOX_TRIAGE_PROFILE_ID
from src.domain.memory_inbox_triage_task import BLOCK_TITLES, INBOX_TRIAGE_HEADER
from tests.support import prompt_render as pr
from tests.support import prompt_vocabulary as pv

TOKEN_ID = "CONSOLIDATION_INBOX_TRIAGE"
PROFILE = pv.load_profile(INBOX_TRIAGE_PROFILE_ID)
DOC = pv.ser.token_from_file(pv.token_path(TOKEN_ID).read_text(encoding="utf-8"))


def test_the_profile_is_the_taxonomy_then_the_triage_token():
    assert PROFILE["tokens"] == {"CONSOLIDATION_TAXONOMY": {"non_overridable": True, "order": 10},
                                 TOKEN_ID: {"non_overridable": True, "order": 20}}
    blueprint = pv.load_blueprint(PROFILE["blueprint_id"])
    assert blueprint["class_order"] == ["taxonomy", DOC["class"]]
    assert pr.rendered_token_ids(INBOX_TRIAGE_PROFILE_ID) == ["CONSOLIDATION_INBOX_TRIAGE", "CONSOLIDATION_TAXONOMY"]


def test_the_static_prompt_holds_the_taxonomy_and_the_triage_token_and_no_fact_writing_rules():
    static = pr.render_static(INBOX_TRIAGE_PROFILE_ID)
    assert "fact_taxonomy" in static and "Trivial_Exclusions" in static and "Inbox_Triage" in static
    assert static.index("fact_taxonomy") < static.index("Inbox_Triage")
    # The rules themselves: the taxonomy only points at Domain_Scope's memory-review line, which the token sets aside.
    for absent in ("rule Tool_Call_Mandatory", "rule Domain_Scope", "Array of operation objects"):
        assert absent not in static
    assert "{" + "{" not in static and "${" not in static


def test_the_triage_texts_fit_the_description_cap():
    texts = [INBOX_TRIAGE_HEADER, *BLOCK_TITLES]
    for declaration in ConsolidationAgent._get_tool_declarations(None, "triage"):  # type: ignore[arg-type]
        texts.append(declaration["description"])
        texts.extend(p.get("description", "") for p in declaration["parameters"]["properties"].values())
    assert max(len(t) for t in texts) <= MAX_TOOL_DESCRIPTION_CHARS
