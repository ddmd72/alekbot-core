"""The memory inbox final-pass prompt (MCP_MEMORY_SUBMISSIONS_RFC §9.1, §9.2): profile `consolidation_inbox`, blueprint
`consolidation_inbox_v1`, the six consolidation tokens of the mail stage then `CONSOLIDATION_INBOX_CANDIDATES`. The
token is Explore-stage, so only its structure is checked: the profile renders through the production assembler with all
seven tokens in order, and the new token names the echo-trap exception by reference, never restating its definition
(`memory_review_reply:` is defined once, in `CONSOLIDATION_POLICIES`; the plain string occurs 3 times in the six
tokens alone, so the check counts the defining key). It reads the same line keys code sends."""
import re

from src.domain.consolidation import EMAIL_PROFILE_ID, INBOX_PROFILE_ID
from src.domain.memory_inbox_final_task import INBOX_CANDIDATES_HEADER, LINE_KEYS
from tests.support import prompt_render as pr
from tests.support import prompt_vocabulary as pv

TOKEN_ID = "CONSOLIDATION_INBOX_CANDIDATES"
PROFILE = pv.load_profile(INBOX_PROFILE_ID)
DOC = pv.ser.token_from_file(pv.token_path(TOKEN_ID).read_text(encoding="utf-8"))
SIX = ("CONSOLIDATION_TAXONOMY", "CONSOLIDATION_COGNITIVE_PROCESS", "CONSOLIDATION_TOOLS", "CONSOLIDATION_EXAMPLES",
       "CONSOLIDATION_POLICIES", "CONSOLIDATION_OUTPUT_SPEC")


def test_the_profile_is_the_mail_stages_six_tokens_then_the_inbox_token():
    email = pv.load_profile(EMAIL_PROFILE_ID)
    assert {k: v for k, v in PROFILE["tokens"].items() if k != TOKEN_ID} == \
           {k: email["tokens"][k] for k in SIX}
    assert PROFILE["tokens"][TOKEN_ID] == {"non_overridable": True, "order": 70}
    blueprint = pv.load_blueprint(PROFILE["blueprint_id"])
    email_blueprint = pv.load_blueprint(email["blueprint_id"])
    assert blueprint["class_order"] == [*email_blueprint["class_order"][:-1], DOC["class"]]
    assert sorted(pr.rendered_token_ids(INBOX_PROFILE_ID)) == sorted([*SIX, TOKEN_ID])


def test_the_static_prompt_renders_the_seven_tokens_and_defines_the_echo_exception_once():
    static = pr.render_static(INBOX_PROFILE_ID)
    assert "rule Domain_Scope" in static and "Inbox_Candidates" in static
    assert static.index("fact_taxonomy") < static.index("rule Domain_Scope") < static.index("Inbox_Candidates")
    # The six tokens name it in an example key and an example rule too; the definition is the one line-leading key.
    assert len(re.findall(r"^\s*memory_review_reply:", static, re.MULTILINE)) == 1
    assert "memory_review_reply" not in static[static.index("Inbox_Candidates"):]
    assert "{" + "{" not in static and "${" not in static


def test_the_token_reads_every_line_key_code_sends():
    body = pv.token_path(TOKEN_ID).read_text(encoding="utf-8").split("rule Inbox_Candidates", 1)[1]
    for key in LINE_KEYS:
        assert f"{key}:" in body, key
    assert "{" not in INBOX_CANDIDATES_HEADER
