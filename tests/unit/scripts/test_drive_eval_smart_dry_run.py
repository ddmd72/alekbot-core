"""G3 harness logic, offline (USER_DRIVE_INDEXING_RFC §17.8, §24.11, D11): drive reads run for real against the
fixture index through a real FileManagementAgent; other read-only delegations go to Smart's own engine; writes are
answered without running; the metric is the search-initiated rate, barred from below on positives and from above on
negatives (final review F5-6)."""
import json
from pathlib import Path

import pytest

from scripts.validation.drive_eval.eval_connection import EvalDriveConnections, eval_connection
from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from scripts.validation.drive_eval.runner import EVAL_USER, EvalClock, index_fixture
from scripts.validation.drive_eval.smart_dry_run import (
    STUB_RESULT, AsEvalUser, DryRunDispatch, G3Error, annotate_drive_read, judge, make_g3_bars, score, searched,
    validate_g3,
)
from scripts.validation.drive_eval.dry_run_drive import DriveOpLog, DryRunDriveOperations
from scripts.validation.drive_eval.specialist_fixture import build_drive_fixture
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.llm import ToolCall
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult
from src.services.drive_search_service import DriveSearchService

from ..services.drive_index_fakes import FakeEmbeddings
from .drive_eval_samples import MANIFEST, fake_content, rome

_GOLD = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval" / "gold_g3.json"
OWNER = "owner-1"


def _case(i, kind="positive", category="find"):
    return {"id": f"c{i}", "set": kind, "category": category, "lang": "en", "text": "question"}


def _valid():
    return [_case(i) for i in range(10)] + [_case(i + 10, "negative", "web") for i in range(10)]


def test_the_committed_gold_is_valid():
    gold = json.loads(_GOLD.read_text(encoding="utf-8"))
    validate_g3(gold)
    assert {c["category"] for c in gold if c["set"] == "positive"} >= {"ocr", "memory_vs_document", "version_family"}
    assert {c["category"] for c in gold if c["set"] == "negative"} == {"chit_chat", "memory", "web", "general"}


@pytest.mark.parametrize("mutate, message", [
    (lambda g: g + [g[0]], "duplicate id c0"),
    (lambda g: [{**g[0], "set": "maybe"}] + g[1:], "set must be"),
    (lambda g: [{**g[0], "category": "web"}] + g[1:], "not a positive category"),
    (lambda g: [{**g[0], "text": " "}] + g[1:], "no text"),
    (lambda g: [{**g[0], "lang": "de"}] + g[1:], "unknown lang"),
    (lambda g: g[:15], "fewer than 10 negative cases"),
])
def test_malformed_gold_is_refused(mutate, message):
    with pytest.raises(G3Error, match=message):
        validate_g3(mutate(_valid()))


@pytest.fixture
async def setup():
    drive = FixtureDrive(MANIFEST)
    repo = InMemoryDriveIndexRepository()
    clock = EvalClock()
    await index_fixture(drive, repo, FakeEmbeddings(), rome, "fake/8", clock, throttle_pause_s=0, **fake_content())
    search = DriveSearchService(repo, FakeEmbeddings(), drive, EvalDriveConnections(eval_connection(EVAL_USER)),
                                clock=clock, embedding_space="fake/8")
    real_calls = []

    async def real_dispatch(engine, tool_call, context, *args, **kwargs):
        if isinstance(engine, DelegationEngine):
            return await DelegationEngine.dispatch(engine, tool_call, context, *args, **kwargs)
        real_calls.append((engine, tool_call.args["intent"]))
        return ToolResult(name=tool_call.name, result_str="real result")

    # the drive side after the move (DRIVE_SPECIALIST_RFC §12): FileManagement (and, with a model, the specialist) on
    # one fixture DriveOperations
    fixture = build_drive_fixture(OWNER, drive, search)
    dispatcher = DryRunDispatch(real_dispatch, fixture.engine, frozenset({"search_memory", "open_file"}))
    return dispatcher, real_calls, fixture


def _call(intent, **context):
    return ToolCall(name="delegate_to_specialist", args={"intent": intent, "query": "q", "context": context})


async def _dispatch(dispatcher, call):
    return await dispatcher("smart-engine", call, {"user_id": OWNER, "account_id": "a"}, {}, {}, "smart_agent_x")


async def test_a_drive_search_runs_against_the_fixture_index(setup):
    dispatcher, real_calls, fixture = setup
    log = DriveOpLog()
    with DryRunDriveOperations(log, annotate=annotate_drive_read):
        result = await fixture.operations.search({"user_id": OWNER}, OWNER, query="Scan_0012")
    assert result.success and "Scan_0012.pdf" in result.text and "<drive_document" in result.text
    assert result.drive_context()
    assert log.entries == [{"op": "search", "args": {"query": "Scan_0012"}, "outcome": "ok", "stubbed": False}]
    assert searched(log.entries) and real_calls == []


@pytest.mark.slow
async def test_list_and_open_from_the_drive_run_on_the_fixture(setup):
    dispatcher, real_calls, fixture = setup
    log = DriveOpLog()
    with DryRunDriveOperations(log):
        listing = await fixture.operations.list_folder({"user_id": OWNER}, OWNER, "Contracts")
        assert "Договор_ред1.docx" in listing.text and listing.success
        ref = f"drive:{item_id_for('Contracts/Договор_ред1.docx')}"
        opened = await _dispatch(dispatcher, _call("open_file", file_ref=ref))
    assert not opened.failed and dispatcher.calls[-1]["route"] == "fixture"
    assert not searched(log.entries) and real_calls == []


async def test_other_reads_go_to_smarts_own_engine_and_writes_never_run(setup):
    dispatcher, real_calls, _ = setup
    memory = await _dispatch(dispatcher, _call("search_memory"))
    chat_file = await _dispatch(dispatcher, _call("open_file", file_ref="report.docx"))
    tasks = await _dispatch(dispatcher, _call("manage_user_tasks", action="add"))
    # a removed drive intent goes to Smart's own engine (the coordinator's unknown-intent refusal), never a stub
    await _dispatch(dispatcher, _call("save_file_to_drive", text="x", name="n.md"))
    assert memory.result_str == chat_file.result_str == "real result"
    assert real_calls == [("smart-engine", "search_memory"), ("smart-engine", "open_file"),
                          ("smart-engine", "save_file_to_drive")]
    assert tasks.result_str == STUB_RESULT.format(intent="manage_user_tasks")
    assert [c["route"] for c in dispatcher.calls] == ["real", "real", "stub", "removed"]


async def test_the_index_is_searched_as_the_eval_user():
    seen = []

    class _Search:
        async def search(self, user_id, request):
            seen.append(user_id)

    await AsEvalUser(_Search()).search(OWNER, None)
    assert seen == [EVAL_USER]


def _rows():
    return [{"set": "positive", "category": "find", "searched": True},
            {"set": "positive", "category": "ocr", "searched": False},
            {"set": "negative", "category": "web", "searched": True},
            {"set": "negative", "category": "web", "searched": False}]


def test_rates_per_set_and_category():
    table = score(_rows())
    assert table["positive"] == {"runs": 2, "searched": 1, "rate": 0.5}
    assert table["negative:web"]["rate"] == 0.5 and table["positive:ocr"]["rate"] == 0.0


def test_the_positives_are_barred_from_below_and_the_negatives_from_above():
    table = score(_rows())
    bars = make_g3_bars(table, "g", "gpt-6-luna", 3, "now")
    assert bars["positive_rate"] == 0.5 and bars["negative_rate_reported"] == 0.5
    # final review F5-6: the negatives are barred too, by the rate's Wilson upper bound
    assert bars["negative_rate_reported"] < bars["negative_bar"] <= 1.0
    # review E minor: the bar is the rate's Wilson lower bound, and judge runs at the recorded --repeats
    assert 0.0 <= bars["positive_bar"] < bars["positive_rate"]
    assert judge(table, bars, "g", "gpt-6-luna", repeats=3) == "pass"
    worse = score([{**r, "searched": False} for r in _rows()])
    assert judge(worse, bars, "g", "gpt-6-luna", repeats=3) == "regression"
    # searching the drive for chit-chat, web or memory questions is the regression on the negatives (F5-6)
    noisier_negatives = score([{**r, "searched": True} if r["set"] == "negative" else r for r in _rows()])
    assert judge(noisier_negatives, bars, "g", "gpt-6-luna", repeats=3) == "regression"
    quieter_negatives = score([{**r, "searched": False} if r["set"] == "negative" else r for r in _rows()])
    assert judge(quieter_negatives, bars, "g", "gpt-6-luna", repeats=3) == "pass"


def test_bars_from_another_gold_or_model_give_no_verdict():
    table = score(_rows())
    bars = make_g3_bars(table, "g", "gpt-6-luna", 3, "now")
    with pytest.raises(G3Error, match="gold_g3.json changed"):
        judge(table, bars, "h", "gpt-6-luna", repeats=3)
    with pytest.raises(G3Error, match="recorded with Smart on gpt-6-luna"):
        judge(table, bars, "g", "claude-sonnet-5-5", repeats=3)
    with pytest.raises(G3Error, match="recorded with --repeats 3"):   # review E minor: equal repeats
        judge(table, bars, "g", "gpt-6-luna", repeats=1)
