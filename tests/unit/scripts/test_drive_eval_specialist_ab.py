"""The drive specialist eval, offline (DRIVE_SPECIALIST_RFC §12, §15): its gold names DriveOperations operations and
their real parameters, the case checks read the drive operation log, the cost stop holds, the before/after comparison
applies the latency bar, and the replay reads commissions from this eval's reports and the spike's. The model
behaviour is what the owner's paid run measures, not these tests."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.validation.drive_eval.fixture_drive import item_id_for
from scripts.validation.drive_eval.smart_dry_run import g3_drive
from scripts.validation.drive_eval.specialist_ab import (
    LATENCY_BAR, AbError, Meter, check_case, compare, history_pair, load_compare_rows, markdown, price, run_all,
    select_cases, summary, validate_ab_gold)
from scripts.validation.drive_eval.specialist_replay import commissions_from_report, content_checks
from src.infrastructure.agent_manifest import Intent

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"
SCAN = "Documents/DOC_20260610_0042.pdf"


def _gold():
    return json.loads((_DIR / "gold_specialist_ab.json").read_text(encoding="utf-8"))


def _op(op, **args):
    return {"op": op, "args": args, "outcome": "ok", "stubbed": op in ("move", "delete", "save_text")}


def test_the_committed_gold_is_valid_on_the_g3_drive():
    gold = _gold()
    drive = g3_drive(_DIR / "tree.json", _DIR / "tree_g3.json", _DIR / "tree_v2.json")
    validate_ab_gold(gold, drive.paths())
    assert len(gold) == 26


@pytest.mark.parametrize("bad,message", [
    ({"ops_must_include": [{"op": "move_file_in_drive", "args": {"folder": "Archive"}}]}, "unknown drive operation"),
    ({"ops_must_include": [{"op": "move", "args": {"drive_folder": "Archive"}}]}, "has no parameter"),
    ({"ops_forbidden": ["delete_file_from_drive"]}, "unknown drive operation"),
    ({"intents_must_include": ["save_file_to_drive"]}, "is a removed drive intent"),
    ({"opened_any": ["Nowhere/missing.pdf"]}, "not on the fixture drive"),
])
def test_the_gold_names_live_operations_parameters_and_paths(bad, message):
    case = {"id": "x", "kind": "find", "turns": ["find it"], "gold": {"drive": True, **bad}}
    with pytest.raises(AbError, match=message):
        validate_ab_gold([case], ["Contracts/a.pdf"])


def test_a_case_is_checked_on_the_drive_log_and_smarts_calls():
    case = {"gold": {"drive": True, "must_open": True, "opened_any": [SCAN], "answer_must_include": [["2031"]],
                     "ops_forbidden": ["delete"], "intents_must_include": [Intent.WORK_WITH_DRIVE_FILES]}}
    calls = [{"intent": Intent.WORK_WITH_DRIVE_FILES, "context": {}}]
    ops = [_op("search", query="identity card"), _op("open", ref=f"drive:{item_id_for(SCAN)}")]
    assert check_case(case, "It expires in 2031.", ops, calls) == []
    assert check_case(case, "It expires in 2031.", ops + [_op("delete", ref="drive:x")], calls) == [
        "forbidden delete ran"]
    assert "no file was opened" in check_case(case, "2031", ops[:1], calls)
    assert check_case(case, "2 031", ops, []) == [f"answer lacks {[['2031']]}", "work_with_drive_files was not called"]


def test_a_recording_link_is_not_an_open_and_a_batch_needs_its_count():
    link_case = {"gold": {"drive": True, "must_not_open": True}}
    assert check_case(link_case, "here", [_op("open", ref="drive:r", original=True)], []) == []
    batch = {"gold": {"drive": True, "ops_must_include": [{"op": "move", "args": {"folder": "Archive"}, "min": 3}]}}
    two = [_op("move", ref=f"drive:{i}", folder="Archive") for i in "ab"]
    assert check_case(batch, "moved", two, []) == ["move {'folder': 'Archive'}: 2 of at least 3"]
    assert check_case(batch, "moved", two + [_op("move", ref="drive:c", folder="archive")], []) == []


def test_a_no_drive_case_fails_on_any_drive_use():
    case = {"gold": {"drive": False}}
    assert check_case(case, "ok", [], [{"intent": "search_memory", "context": {}}]) == []
    assert check_case(case, "ok", [], [{"intent": Intent.WORK_WITH_DRIVE_FILES, "context": {}}]) == [
        "the drive was used: ['work_with_drive_files']"]
    assert check_case(case, "ok", [], [{"intent": "save_file_to_drive", "context": {}}]) == [
        "the drive was used: ['save_file_to_drive']"]


def test_the_meter_prices_known_models_and_charges_unknown_ones_the_fallback_rate():
    usd, priced = price("claude-sonnet-5-5", 1_000_000, 100_000, 0, 0)
    assert priced and usd == pytest.approx(3.0)
    assert price("no-such-model", 1_000_000, 0, 0, 0) == (5.0, False)
    meter = Meter(max_usd=1.0)
    meter.record("drive", "claude-sonnet-5-5", SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=0), 1.0)
    with pytest.raises(Exception, match="max-usd"):
        meter.check()
    assert meter.capped


async def test_the_cost_stop_skips_the_next_case_when_the_dearest_would_pass_the_cap():
    meter = Meter(max_usd=1.0)
    ran = []

    async def one(case, *, repeat):
        ran.append(case["id"])
        meter.spent += 0.6
        return {"id": case["id"], "correct": True, "round_trips": 1, "smart_llm_calls": 1, "specialist_llm_calls": 1,
                "wall_s": 1.0, "usd": 0.6, "failures": []}

    rows, stopped = await run_all([{"id": "a"}, {"id": "b"}], 1, run_one=one, meter=meter)
    assert ran == ["a"] and "would pass --max-usd 1.0" in stopped


async def test_the_owner_run_sends_each_repeat_to_its_case(monkeypatch, tmp_path):
    """Re-review of part B (mutant #11 of specialist_ab, not equivalent): `_run` hands run_case the repeat it is on,
    so each repeat gets its own session (`…:<case>:<repeat>`) and its rows are labelled; dropping it would replay
    repeat 0's history into every later repeat. The world (Firestore, models) is a stand-in; nothing is called."""
    import argparse
    from scripts.validation.drive_eval import specialist_ab as ab
    seen = []
    world = SimpleNamespace(smart=object(), dispatcher=object(), drive_log=object(), sessions=object(),
                            meter=Meter(max_usd=5.0), user_id="u1", account_id="a1", restore=[])

    async def build_world(args, gold):
        return world

    async def run_case(case, **kw):
        seen.append((case["id"], kw["repeat"], kw["user_id"], kw["account_id"]))
        return {"id": case["id"], "repeat": kw["repeat"], "correct": True, "round_trips": 1, "smart_llm_calls": 1,
                "specialist_llm_calls": 1, "wall_s": 1.0, "usd": 0.0, "failures": []}

    monkeypatch.setattr(ab, "build_world", build_world)
    monkeypatch.setattr(ab, "run_case", run_case)
    monkeypatch.setattr(ab, "write_report", lambda rows, meta, comparison: (tmp_path / "r.json", tmp_path / "r.md"))
    monkeypatch.setattr(ab, "markdown", lambda rows, meta, comparison: "")
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps([{"id": "c1"}, {"id": "c2"}]), encoding="utf-8")
    args = argparse.Namespace(gold=gold, cases="", repeats=2, compare=None, compare_design="", smart_model="",
                              max_usd=5.0)
    assert await ab._run(args) == ab.EXIT_OK
    assert seen == [("c1", 0, "u1", "a1"), ("c2", 0, "u1", "a1"), ("c1", 1, "u1", "a1"), ("c2", 1, "u1", "a1")]


def _rows(walls, correct=True):
    return [{"id": f"c{i}", "correct": correct, "wall_s": w, "smart_llm_calls": 2, "specialist_llm_calls": 3,
             "usd": 0.1} for i, w in enumerate(walls)]


def test_the_comparison_applies_the_latency_bar_to_the_shared_cases():
    before, after = _rows([10.0, 20.0, 30.0]), _rows([12.0, 24.0, 36.0])
    result = compare(before, after)
    assert result["before"]["wall_p50_s"] == 20.0 and result["after"]["wall_p50_s"] == 24.0
    assert result["latency_ok"] and LATENCY_BAR == 1.25
    assert not compare(before, _rows([12.0, 26.0, 36.0]))["latency_ok"]
    after[0]["correct"] = False
    assert compare(before, after)["changed"] == [{"id": "c0", "before": True, "after": False}]
    assert compare(before, after + [{"id": "only-after", "correct": True, "wall_s": 1.0}])["after"]["cases"] == 3


def test_a_spike_report_is_read_by_design(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"rows": [{"id": "a", "design": "today"}, {"id": "a", "design": "specialist"}]}))
    assert [r["design"] for r in load_compare_rows(path, "today")] == ["today"]
    assert len(load_compare_rows(path)) == 2


def test_the_replay_reads_this_evals_reports_and_the_spikes():
    turns = [{"text": "Find my leases.", "delegations": ["work_with_drive_files"]},
             {"text": "Open the second one.", "delegations": ["search_memory", "work_with_drive_files"]}]
    calls = [{"intent": "work_with_drive_files", "route": "fixture", "query": "find leases", "context": {}},
             {"intent": "search_memory", "route": "real", "query": "", "context": {}},
             {"intent": "work_with_drive_files", "route": "fixture", "query": "open drive:BBB",
              "context": {"file_contents": []}}]
    ours = commissions_from_report({"rows": [{"id": "case", "turns": turns, "calls": calls}]})
    assert [(c["n"], c["turn"], c["user_message"], c["query"]) for c in ours] == [
        (1, 0, "Find my leases.", "find leases"), (2, 1, "Open the second one.", "open drive:BBB")]
    spike = {"rows": [{"id": "case", "design": "specialist", "turns": turns[:1],
                       "calls": [{**calls[0], "route": "specialist"}]},
                      {"id": "case", "design": "today", "turns": turns[:1], "calls": []}]}
    assert len(commissions_from_report(spike, "specialist")) == 1


def test_the_replays_content_checks_read_the_delivered_text_and_the_log():
    gold = {"answer_must_include": [["249"]], "ops_must_include": [{"op": "search", "args": {"query": "FA-0137"}}]}
    assert content_checks(gold, "Total: 249,00 EUR", [_op("search", query="Factura FA-0137")]) == []
    assert content_checks(gold, "Total: 249,00 EUR", [_op("search", query="locksmith")]) == [
        "search {'query': 'FA-0137'}: 0 of at least 1"]


def test_the_history_pair_keeps_the_drive_refs_in_full_text_only():
    user, model = history_pair("find my leases", "Two leases.", {"drive_context": [[{"path": "a", "ref": "drive:A"}]],
                                                                 "drive_status": "ok"})
    assert model.parts[0].text == "Two leases." and model.parts[0].full_text.endswith(
        '{"drive_context":[[{"path":"a","ref":"drive:A"}]]}')


def test_unknown_case_ids_are_refused():
    with pytest.raises(AbError, match="unknown case ids"):
        select_cases(_gold(), "ab-find-nda,no-such-case")


def test_the_report_shows_the_comparison_and_calls_to_removed_intents():
    row = {**_rows([5.0])[0], "round_trips": 1, "drive_statuses": ["ok"], "removed_intent_calls": 1,
           "other_llm_calls": 0, "failures": [], "usd_by_model": {"claude-sonnet-5-5": 0.1}, "unpriced_models": [],
           "tokens": {g: {"calls": 0, "input": 0, "cached": 0, "output": 0, "usd": 0.0}
                      for g in ("smart", "specialist", "other")}}
    meta = {"stamp": "s", "smart_model": "gpt-6.1-sol", "spent": 0.1, "max_usd": 3}
    text = markdown([row], meta, compare(_rows([4.0]), [row]))
    assert "Calls to removed drive intents: 1" in text and "Latency bar" in text and "| after | 1 |" in text
    assert summary([row])["wall_p50_s"] == 5.0


async def test_the_in_memory_sessions_keep_each_sessions_id():
    from scripts.validation.drive_eval.specialist_ab import InMemorySessions
    from src.domain.llm import Message, MessagePart

    sessions = InMemorySessions()
    assert (await sessions.load_session("u1:c1")).session_id == "u1:c1"
    await sessions.append_message("u1:c2", Message(role="user", parts=[MessagePart(text="hi")]))
    assert (await sessions.load_session("u1:c2")).session_id == "u1:c2"


def test_the_history_pair_keeps_the_users_text():
    user, _ = history_pair("find my leases", "Two leases.", {})
    assert user.parts[0].text == "find my leases"


async def test_a_case_sends_smart_each_turn_as_a_chat_turn_of_the_user():
    """What run_case gives Smart per turn: the text, the user's ids, an in-memory session per case and repeat, the
    turn's message parts, and interactive_delivery (Smart then declares the chat-only tools the bar covers)."""
    from scripts.validation.drive_eval.dry_run_drive import DriveOpLog
    from scripts.validation.drive_eval.smart_dry_run import DryRunDispatch
    from scripts.validation.drive_eval.specialist_ab import InMemorySessions, run_case

    seen = []

    class Smart:
        agent_id = "smart_agent_u1"

        async def execute(self, message):
            seen.append(message)
            return SimpleNamespace(result="Done.", metadata={})

    case = {"id": "c1", "kind": "find", "turns": ["find my lease", "open the second"], "gold": {"drive": False}}
    row = await run_case(case, smart=Smart(), dispatcher=DryRunDispatch(None, None, frozenset()),
                         drive_log=DriveOpLog(), meter=Meter(), sessions=InMemorySessions(), user_id="u1",
                         account_id="a1", repeat=2)
    assert [m.payload["text"] for m in seen] == ["find my lease", "open the second"]
    for message, text in zip(seen, case["turns"]):
        ctx = message.context
        assert (ctx["user_id"], ctx["account_id"], ctx["session_id"]) == ("u1", "a1", "u1:drive_specialist_eval:c1:2")
        assert [p.text for p in ctx["current_message_parts"]] == [text] and ctx["interactive_delivery"] is True
    assert row["repeat"] == 2 and row["correct"]


def test_a_setup_problem_is_no_run_with_its_own_exit_code(capsys):
    """An unknown case id is refused before anything is built or paid for: exit 2, the reason on stderr."""
    from scripts.validation.drive_eval.specialist_ab import EXIT_SETUP, main

    assert main(["--max-usd", "1", "--cases", "no-such-case"]) == EXIT_SETUP
    assert "no run" in capsys.readouterr().err
