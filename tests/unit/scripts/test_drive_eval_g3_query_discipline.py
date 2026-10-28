"""G3 harness after the 2026-10-19 run: v2 cases isolated from the owner's memory and mail, the D29 detector reading all
four renderings, turn-cap runs as their own category; and Smart's half of the former drive-search skill's query
discipline (the specialist's token half is not pinned: owner decision 2026-10-28, its wording is tuned on evals)."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.validation.drive_eval import smart_dry_run as g3
from src.domain import drive_answer_texts as t
from src.domain import drive_search_texts as T
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.delegation_engine import ToolResult

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"


def _tc(intent, context=None):
    return SimpleNamespace(name="delegate_to_specialist", args={"intent": intent, "context": context or {}})


class _Real:
    def __init__(self, text="REAL"):
        self.text, self.seen = text, []

    async def __call__(self, engine, tool_call, context, *a, **kw):
        self.seen.append(tool_call.args["intent"])
        return ToolResult(name=tool_call.name, result_str=self.text)


def _dispatch(real, isolate):
    return g3.DryRunDispatch(real, object(), frozenset({Intent.SEARCH_MEMORY, Intent.SEARCH_EMAILS, "search_web"}),
                             isolate=isolate)


# -- isolation ----------------------------------------------------------------------------------------------------

def test_every_v2_case_is_isolated_and_the_flag_validates():
    gold = json.loads((_DIR / "gold_g3.json").read_text(encoding="utf-8"))
    v2 = json.loads((_DIR / "gold_g3_v2.json").read_text(encoding="utf-8"))
    assert v2 and all(c["id"].startswith("v2-") and c.get("isolate") is True for c in v2)
    g3.validate_g3(gold + v2)
    with pytest.raises(g3.G3Error, match="isolate"):
        g3.validate_g3(gold + v2 + [dict(v2[0], id="x", isolate="yes")])


@pytest.mark.parametrize("intent", [Intent.SEARCH_MEMORY, Intent.SEARCH_EMAILS])
async def test_an_isolated_case_gets_empty_memory_and_mail_without_a_real_call(intent):
    real = _Real()
    d = _dispatch(real, True)
    result = await d(object(), _tc(intent), {})
    assert result.result_str == g3.EMPTY_RECORDS and real.seen == []
    assert d.calls[0]["route"] == "isolated"


async def test_isolation_leaves_other_reads_and_other_cases_alone():
    real = _Real()
    d = _dispatch(real, True)
    assert (await d(object(), _tc("search_web"), {})).result_str == "REAL"
    d.isolate = False
    assert (await d(object(), _tc(Intent.SEARCH_MEMORY), {})).result_str == "REAL"
    assert real.seen == ["search_web", Intent.SEARCH_MEMORY]


# -- D29 detector -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("line", [T.ABSENT_LINE, T.ABSENT_IN_SCOPE_LINE, T.SEARCHED_ABSENT_LINE,
                                  T.SEARCHED_ABSENT_IN_SCOPE_LINE])
async def test_every_rendering_of_the_absent_word_line_is_a_d29(line):
    text = "Results.\n" + line.format(word="vaccination") + "\nWithout ..."
    assert g3.is_d29(text)
    # the mark moved from Smart's dispatch to the drive operation log (DryRunDriveOperations' annotate hook)
    outcome = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text=text, success=True)
    assert g3.annotate_drive_read("search", outcome) == {"d29": True}


def test_other_lines_are_not_a_d29():
    assert not g3.is_d29("No file matches all of: \"a\" (3 files).") and not g3.is_d29(None)


# -- turn cap -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("answer, capped", [(None, True), ("", True), ("None", True), ("  ", True), ("Found it", False)])
def test_turn_capped(answer, capped):
    assert g3.turn_capped(answer) is capped


def test_a_turn_cap_run_is_its_own_category_and_a_hard_failure():
    row = {"id": "v2-x", "repeat": 1, "calls": [], "turn_cap": True}
    ok = {"id": "v2-y", "repeat": 0, "calls": []}
    assert g3.turn_cap_runs([row, ok]) == ["v2-x (run 1)"]
    assert g3.hard_failures([row, ok]) == ["v2-x (run 1): no answer (turn cap)"]


# -- the skill's query discipline -----------------------------------------------------------------------------------

# Reviewer (owner decision 2026-10-28): test_the_skill_states_the_query_discipline kept only its Smart-side check (the
# reading line in src/domain/drive_answer_texts.py); its four PROTOCOL_DRIVE_SEARCH sentences pinned the specialist's
# token word for word and were removed.
def test_the_all_is_still_a_find_reading_line_is_said_once():
    # Smart's half of the "all is still a find" rule reaches Smart as a reading line (§5.7, §7.3)
    assert re.sub(r"\s+", " ", t.R_ALL).count(
        '"All", "every", "everything about" is still a find: answer from the cards; do not open files or compute '
        "totals unless the user asked for a total or a value.") == 1


def test_the_manifest_query_text_ends_with_the_discipline_sentence():
    # Reviewer edit at the cut: the field text is the specialist's `search_drive.query` (DRIVE_SPECIALIST_RFC §7.6)
    from src.domain import drive_tool_texts as tt
    assert tt.QUERY.endswith("All words must match: use the user's few key words in one language.")


# -- Smart runs as an interactive turn: under the turn clock -----------------------------------------------------------

def test_the_harness_clock_is_the_interactive_one():
    from src.domain.turn_clock import LONG_TURN_MAX_LOOP_TURNS
    clock = g3.interactive_turn_clock()
    assert clock.max_loop_turns == LONG_TURN_MAX_LOOP_TURNS == 40
    assert clock.orchestrator_agent_type == "smart_response" and clock.remaining() > clock.wrap_up_reserve_s


def test_the_run_sets_the_clock_around_smart_execute():
    import inspect
    src = inspect.getsource(g3._run)
    set_at, exec_at, reset_at = (src.index(t) for t in (
        "CURRENT_TURN_CLOCK.set(interactive_turn_clock())", "await smart.execute(msg)", "CURRENT_TURN_CLOCK.reset("))
    assert set_at < exec_at < reset_at
