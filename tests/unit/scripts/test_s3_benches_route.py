"""Facts S3 · T12b: each bench's `--s3-kit` message runs the pass its gate reads, in the owner's zone.

The builders are called with no model and no network. Each message then goes through the real agent on the kit over the
evaluating Firestore fake with a scripted LLM boundary, and the assertions read what the kit reports: the pass labels and
actors the gates read (`stage1`/`stage2a`/`stage2b`, `cluster`, `cluster`, `stage2b`, `email`) and the owner's zone.
"""
import ast
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.consolidation import s3_bench_support as support
from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation import test_cluster_audit_dryrun as bench2
from scripts.consolidation import test_decomposition_dryrun as bench3
from scripts.consolidation import test_directive_review_dryrun as bench4
from scripts.consolidation import test_stage1_classification_dryrun as bench1
from scripts.consolidation.s3_dry_run_kit import DryRunKit, LedgerBusy
from scripts.email import test_consolidation_dryrun as bench5
from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentResponse
from src.domain.llm import ToolCall
from src.domain.entities import FactDomain, FactState
from tests.support.fact_kit import ACC, USER
from tests.support.fact_write import EXPIRY_NONE, horizon
from tests.support.firestore_eval import EvalDb
from tests.unit.agents.test_consolidation_write_context import OMIT, calls, create, email, make_agent
from tests.unit.scripts.test_s3_dry_run_kit import (  # noqa: F401  (fixtures: store, memory_dir)
    SONNET,
    answered,
    assembly,
    email_store,
    kit_agent,
    memory_dir,
    metered,
    scripted,
    seed,
    store,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BENCH_FILES = [
    PROJECT_ROOT / "scripts" / "consolidation" / "test_stage1_classification_dryrun.py",
    PROJECT_ROOT / "scripts" / "consolidation" / "test_cluster_audit_dryrun.py",
    PROJECT_ROOT / "scripts" / "consolidation" / "test_decomposition_dryrun.py",
    PROJECT_ROOT / "scripts" / "consolidation" / "test_directive_review_dryrun.py",
    PROJECT_ROOT / "scripts" / "email" / "test_consolidation_dryrun.py",
]
MADRID = "Europe/Madrid"


def invalidate(fact_id: str) -> ToolCall:
    return ToolCall(name="update_fact", args={"fact_id": fact_id, "updates": {"state": "invalidated"}})


# ----------------------------------------------------------------------------------------------- importing builds nothing

_SIDE_EFFECT_CALLS = {"load_dotenv", "load_env", "AsyncClient", "load_settings", "ServiceContainer", "UserAgentFactory", "run"}


@pytest.mark.parametrize("path", BENCH_FILES, ids=lambda p: p.name)
def test_no_bench_runs_anything_at_module_level_except_under_main(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
            continue
        if isinstance(statement, ast.If) and "__main__" in ast.dump(statement.test):
            continue
        for node in ast.walk(statement):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                assert name not in _SIDE_EFFECT_CALLS, f"{path.name}:{statement.lineno} calls {name} at import"


@pytest.mark.slow
def test_importing_every_bench_in_a_fresh_process_builds_no_client_and_reads_no_dotenv():
    code = (
        "import dotenv, importlib\n"
        "from google.cloud import firestore\n"
        "def boom(*a, **k): raise SystemExit('forbidden at import')\n"
        "dotenv.load_dotenv = boom\n"
        "firestore.AsyncClient = boom\n"
        "for name in ('scripts.consolidation.s3_bench_support', 'scripts.consolidation.test_stage1_classification_dryrun',"
        " 'scripts.consolidation.test_cluster_audit_dryrun', 'scripts.consolidation.test_decomposition_dryrun',"
        " 'scripts.consolidation.test_directive_review_dryrun', 'scripts.email.test_consolidation_dryrun'):\n"
        "    importlib.import_module(name)\n"
        "print('imported')\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env={"PYTHONPATH": str(PROJECT_ROOT), "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0 and "imported" in done.stdout, done.stderr[-1500:]


def test_load_env_is_the_only_place_a_bench_reads_dotenv(monkeypatch):
    import dotenv

    seen = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: seen.append(1))
    support.load_env()
    assert seen == [1]


# ----------------------------------------------------------------------------------------------- bench 1: stage1, stage2a, stage2b


async def test_bench_1_sends_a_stage_1_consolidate_and_the_run_passes_are_stage1_stage2a_stage2b(store, tmp_path):
    await seed(store, "walks daily and reads at night")
    rule = await seed(store, "always answer in English", domain="agent_directive")
    agent, kit = await kit_agent(
        store, tmp_path,
        metered(calls(create("flight booked", expiry=horizon(30)))), answered(),
        metered(calls(create("reads at night", expiry=EXPIRY_NONE))), answered(),
        metered(calls(invalidate(rule))), answered(),
    )
    agent.INLINE_CLUSTER_REVIEW = True
    kit.timezone = MADRID
    turns = [{"role": "user", "text": "booked a flight", "timestamp": 1.0, "created_at": datetime.now(timezone.utc).timestamp()}]

    message = bench1.build_stage1_message(kit, turns, [], USER, ACC)
    response = await support.execute(agent, message, USER, ACC, "Stage 1")

    assert message.payload["task"] == "consolidate" and message.payload["messages"] == turns
    assert message.context["owner_timezone"] == MADRID and message.context["user_id"] == USER
    assert response.status.value == "success"
    assert [p["pass"] for p in kit.passes] == ["stage1", "stage2a", "stage2b"]
    assert [(r["pass"], r["actor"], r["action"]) for r in kit.rows] == [
        ("stage1", "stage1", "create"), ("stage2a", "stage2a", "create"), ("stage2b", "stage2b", "update")]
    assert kit.report()["timezone"] == MADRID


async def test_bench_1_window_keeps_the_stored_message_times_only_for_the_kit():
    class Sessions:
        def collection(self, name):
            return self

        async def stream(self):
            yield SimpleNamespace(id=f"{USER}:c1", to_dict=lambda: {
                "owner_id": USER, "last_activity": 5.0,
                "messages": [{"role": "user", "created_at": 1_760_000_000.0, "parts": [{"text": "booked a flight"}]},
                             {"role": "model", "parts": [{"text": "noted"}]}],
            })

    kept, _ = await bench1.load_window(Sessions(), USER, 10, keep_times=True)
    plain, _ = await bench1.load_window(Sessions(), USER, 10)

    assert [t.get("created_at") for t in kept] == [1_760_000_000.0, None]       # a turn with no stored time keeps "now"
    assert all("created_at" not in t for t in plain)                            # the bench's own mode is unchanged
    assert all("timestamp" in t for t in kept + plain)


# ----------------------------------------------------------------------------------------------- bench 2: cluster


async def test_bench_2_sends_a_consolidate_cluster_over_an_explicit_cluster_and_the_pass_is_cluster(store, tmp_path):
    long_id = await seed(store, "walks daily and reads at night and cooks on sundays with the whole family every week")
    near_id = await seed(store, "cooks on sundays")
    agent, kit = await kit_agent(store, tmp_path, metered(calls(create("walks daily", expiry=EXPIRY_NONE))), answered())
    kit.timezone = MADRID
    fact = await store.repo.get_fact_by_id(long_id)

    cluster = await bench2.build_batch_cluster(agent, [fact], kit.timezone, USER, ACC)
    message = bench2.build_cluster_audit_message(kit, cluster, [], USER, ACC, batch_idx=3)
    response = await support.execute(agent, message, USER, ACC, "consolidate_cluster")

    assert [e["fact_id"] for e in cluster][0] == long_id and near_id in {e["fact_id"] for e in cluster}
    assert {"fact_id", "content", "similarity", "expiry", "due"} <= set(cluster[0])
    assert message.payload["task"] == "consolidate_cluster" and message.payload["cluster"] == cluster
    assert message.context["owner_timezone"] == MADRID and message.context["session_id"] == "cluster_audit_batch3"
    assert response.status.value == "success"
    assert [p["pass"] for p in kit.passes] == ["cluster"]
    assert [(r["pass"], r["actor"]) for r in kit.rows] == [("cluster", "cluster")]


async def test_the_cluster_keeps_its_seeds_first_and_adds_search_results_longest_first_without_duplicates():
    seen = []

    async def search(*, keywords, primary_query, alternative_query, limit, timezone):
        seen.append((keywords, primary_query, timezone))
        return {
            "alpha beta gamma": [
                {"fact_id": "s1", "content": "seed one again", "similarity": 0.9},
                {"fact_id": "x1", "content": "one two", "similarity": 0.2},
                {"fact_id": "x2", "content": "one two three four five", "similarity": 0.4},
            ],
            "delta": [{"fact_id": "x1", "content": "one two", "similarity": 0.8}, {"fact_id": "x3", "content": "a", "similarity": 0.1}],
        }[primary_query]

    seeds = [{"fact_id": "s1", "content": "seed one", "similarity": 0.0, "expiry": None, "due": None},
             {"fact_id": "s2", "content": "seed two", "similarity": 0.0, "expiry": None, "due": None}]

    cluster = await support.cluster_from_search(
        search, ["alpha beta gamma", "delta", ""], seeds, MADRID, user_id=USER, account_id=ACC, limit=4)

    assert [e["fact_id"] for e in cluster] == ["s1", "s2", "x2", "x1"]            # seeds as given, then longest first, capped
    assert cluster[0]["content"] == "seed one" and cluster[3]["similarity"] == 0.8      # the seed is kept; the higher similarity wins
    assert [s[1] for s in seen] == ["alpha beta gamma", "delta"] and seen[0][0] == ["alpha", "beta", "gamma"]
    assert all(s[2] == MADRID for s in seen)
    assert len(await support.cluster_from_search(
        search, ["delta"], seeds * 3, MADRID, user_id=USER, account_id=ACC, limit=1)) == 6   # never fewer than the seeds


# ----------------------------------------------------------------------------------------------- bench 3: stage 1, then cluster


async def test_bench_3_runs_stage_1_then_a_consolidate_cluster_over_the_facts_stage_1_wrote(store, tmp_path):
    await seed(store, "walks daily and reads at night")
    agent, kit = await kit_agent(
        store, tmp_path,
        metered(calls(create("walks daily", expiry=EXPIRY_NONE), create("reads at night", expiry=EXPIRY_NONE))), answered(),
        metered(calls(create("reads before bed", expiry=EXPIRY_NONE))), answered(),
    )
    kit.timezone = MADRID

    stage1 = bench3.build_size_review_message(kit, "SYSTEM MAINTENANCE — SIZE REVIEW", [], USER, ACC)
    await support.execute(agent, stage1, USER, ACC, "Stage 1")
    written = await support.written_entries(kit, 0, kit.timezone)
    cluster = await support.cluster_from_search(
        agent._fact_management.search_existing_facts, [e["content"] for e in written], written, kit.timezone,
        user_id=USER, account_id=ACC)
    stage2 = bench3.build_stage2_cluster_message(kit, cluster, [], USER, ACC)
    await support.execute(agent, stage2, USER, ACC, "consolidate_cluster")

    assert stage1.payload["task"] == "consolidate" and stage1.payload["messages"][0]["text"].startswith("SYSTEM MAINTENANCE")
    assert stage2.payload["task"] == "consolidate_cluster"
    assert stage1.context["owner_timezone"] == MADRID == stage2.context["owner_timezone"]
    assert {e["content"] for e in written} == {"walks daily", "reads at night"}
    assert [e["fact_id"] for e in cluster[:2]] == [e["fact_id"] for e in written]        # the written facts are the seeds
    assert [p["pass"] for p in kit.passes] == ["stage1", "cluster"]
    assert [(r["pass"], r["actor"]) for r in kit.rows] == [("stage1", "stage1")] * 2 + [("cluster", "cluster")]


async def test_written_entries_leave_out_what_was_retired_and_what_failed(store, tmp_path):
    compound = await seed(store, "walks daily and reads at night")
    part = create("walks daily", expiry=EXPIRY_NONE)
    agent, kit = await kit_agent(store, tmp_path, metered(calls(part, create("no expiry", expiry=OMIT))), answered())

    await support.execute(agent, bench3.build_size_review_message(kit, "x", [], USER, ACC), USER, ACC, "Stage 1")
    await kit.repo.invalidate_fact(next(r["fact_id"] for r in kit.rows if r["status"] == "created"), ACC)

    assert any(r["status"] == "failed" for r in kit.rows)
    assert await support.written_entries(kit, 0, "UTC") == []
    assert (await kit.repo.get_fact_by_id(compound)).state is FactState.CURRENT


# ----------------------------------------------------------------------------------------------- bench 4: stage2b


async def test_bench_4_calls_review_directives_with_the_stage2b_context_and_the_owner_zone(store, tmp_path):
    first = await seed(store, "always answer in English", domain="agent_directive")
    second = await seed(store, "never use tables", domain="agent_directive", context_priority="high")
    agent, kit = await kit_agent(store, tmp_path, metered(calls(invalidate(first))), answered(),
                                 metered(calls(invalidate(first))), answered())
    kit.timezone = MADRID

    call = bench4.build_review_call(kit, [], USER, ACC)
    recorder = bench4.OperationRecorder(agent._fact_management)
    with_ctx = await agent._review_directives(call.user_id, call.account_id, call.biographical_facts, call.write_context,
                                              fact_management=recorder)

    assert (call.write_context.actor, call.write_context.timezone, call.write_context.model_dates) == ("stage2b", MADRID, False)
    assert with_ctx == [] and [p["pass"] for p in kit.passes] == ["stage2b"]
    assert [(r["pass"], r["actor"], r["action"]) for r in kit.rows] == [("stage2b", "stage2b", "update")]
    assert recorder.operations == [{"action": "INVALIDATE", "fact_id": first, "content": "", "state": "invalidated"}]
    rulebook = {first: "always answer in English", second: "never use tables"}
    assert bench4.classify_run(rulebook, recorder.operations)[first]["verdict"] == "INVALIDATED"

    # run 2 must see the live rulebook again, not run 1's changes
    assert [f.id for f in await agent._repo.get_active_facts_ordered(ACC, domain=FactDomain.AGENT_DIRECTIVE.value)] == [second]
    kit.new_run()
    assert {f.id for f in await agent._repo.get_active_facts_ordered(ACC, domain=FactDomain.AGENT_DIRECTIVE.value)} == {first, second}
    assert [w["kind"] for w in kit.repo.writes] == ["update_fact_fields"]                    # the record of writes stays


async def test_forgetting_the_overlay_keeps_the_record_of_writes_and_the_live_store_untouched(store, tmp_path):
    fact = await seed(store, "always answer in English", domain="agent_directive")
    agent, kit = await kit_agent(store, tmp_path, answered())
    await agent._fact_management.update_fact(fact, {"state": "invalidated"}, ACC, context=kit.write_context("stage2b"))
    assert (await kit.repo.get_fact_by_id(fact)).state is FactState.INVALIDATED

    kit.new_run()

    assert (await kit.repo.get_fact_by_id(fact)).state is FactState.CURRENT
    assert [w["kind"] for w in kit.repo.writes] == ["update_fact_fields"]
    assert (await store.repo.get_fact_by_id(fact)).state is FactState.CURRENT


# ----------------------------------------------------------------------------------------------- bench 5: email


async def test_bench_5_sends_a_consolidate_email_pass_and_the_pass_is_email(tmp_path):
    store, live_emails = email_store()
    await live_emails.save_batch([email("e1", datetime(2026, 10, 10, 9, 0), datetime(2026, 10, 13, 1)),
                                  email("e2", datetime(2026, 10, 12, 9, 0), datetime(2026, 10, 13, 2))])
    agent, kit = await kit_agent(store, tmp_path, metered(calls(create("flight booked", expiry=horizon(30)))), answered(),
                                 email_repo=live_emails)
    kit.timezone = MADRID

    assert await bench5.has_unconsolidated_emails(agent._indexed_email_repo, USER)
    message = bench5.build_email_message(kit, [], USER, ACC, 30)
    response = await support.execute(agent, message, USER, ACC, "consolidate_email")

    assert message.payload["task"] == "consolidate_email"
    assert (message.payload["number_of_batches"], message.payload["batch_size"]) == (1, 30)
    assert message.context["owner_timezone"] == MADRID
    assert response.result["batches_processed"] == 1
    assert [p["pass"] for p in kit.passes] == ["email"] and [(r["pass"], r["actor"]) for r in kit.rows] == [("email", "email")]
    assert [e.email_id for e in await live_emails.get_unconsolidated_batch(USER)] == ["e1", "e2"]      # nothing marked live
    assert [w["email_ids"] for w in kit.emails.writes] == [["e1", "e2"]]


async def test_bench_5_says_so_when_there_is_no_unconsolidated_email():
    _, live_emails = email_store()

    assert await bench5.has_unconsolidated_emails(live_emails, USER) is False


# ----------------------------------------------------------------------------------------------- support: failure, report, gates


async def test_a_failed_response_stops_the_bench_instead_of_going_on(store, tmp_path):
    agent, kit = await kit_agent(store, tmp_path, answered(), prepare=False)         # a pass before prepare() fails inside execute()
    message = bench1.build_stage1_message(kit, [{"role": "user", "text": "hi", "timestamp": 1.0}], [], USER, ACC)

    with pytest.raises(support.KitRunFailed, match="prepare"):
        await support.execute(agent, message, USER, ACC, "Stage 1")
    with pytest.raises(support.KitRunFailed, match="boom"):
        support.require_success(AgentResponse.failure(task_id="t", agent_id="a", error="boom"), "x")
    assert kit.rows == []


def bench_agent(store, *responses, email_repo=None):
    agent = make_agent(store, scripted(*responses), email_repo=email_repo)
    agent.model_name = SONNET
    agent.prompt_builder.assembly_service = assembly()
    agent._lifecycle_counters = FirestoreFactLifecycleCounters(EvalDb(), EnvironmentConfig())
    return agent


def handles_for(agent, store=None, db=None):
    container = SimpleNamespace(repository=store.repo) if store is not None else None
    return support.BenchHandles(db=db, config={}, container=container, account_repo=None, user_repo=None, agent=agent)


async def test_the_kit_session_installs_the_kit_reports_and_releases_the_ledger_even_when_the_body_fails(store, tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "SPEND_LEDGER", tmp_path / "spend.json")
    agent = bench_agent(store, metered(calls(create("flight booked", expiry=horizon(30)))), answered())
    args = SimpleNamespace(budget_usd=30.0, timezone=MADRID)

    with pytest.raises(support.KitRunFailed):
        async with support.kit_session(handles_for(agent), args, USER, ACC, "probe") as (kit, extra):
            extra["marker"] = "set by the body"
            await support.execute(agent, bench5.build_email_message(kit, [], USER, ACC, 5), USER, ACC, "consolidate_email")
            await agent.execute(bench1.build_stage1_message(kit, [{"role": "user", "text": "hi", "timestamp": 1.0}], [], USER, ACC))
            raise support.KitRunFailed("the body stopped")

    (path,) = list(tmp_path.glob("s3_dry_run_probe_*.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["bench"] == {"name": "probe", "marker": "set by the body"} and report["timezone"] == MADRID
    assert [t["id"] for t in report["served_tokens"]] == list(kit_module.S3_TOKEN_IDS)
    DryRunKit(ledger_path=tmp_path / "spend.json").close()                       # the lock was released by the session


async def test_the_kit_session_refuses_an_agent_the_kit_cannot_run_on_and_still_frees_the_ledger(store, tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "SPEND_LEDGER", tmp_path / "spend.json")
    agent = bench_agent(store, answered())
    agent.prompt_version = "v2"

    with pytest.raises(ValueError, match="v3"):
        async with support.kit_session(handles_for(agent), SimpleNamespace(budget_usd=30.0, timezone=MADRID), USER, ACC, "probe"):
            pytest.fail("the body must not run")
    DryRunKit(ledger_path=tmp_path / "spend.json").close()
    assert list(tmp_path.glob("s3_dry_run_probe_*.json")) == []


async def test_a_second_kit_in_the_same_process_waits_for_the_first(store, tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "SPEND_LEDGER", tmp_path / "spend.json")
    args = SimpleNamespace(budget_usd=30.0, timezone=MADRID)

    async with support.kit_session(handles_for(bench_agent(store, answered())), args, USER, ACC, "first"):
        with pytest.raises(LedgerBusy):
            DryRunKit(ledger_path=tmp_path / "spend.json")


def test_the_report_goes_under_the_memory_directory_and_carries_the_bench_part(tmp_path):
    kit = DryRunKit(ledger_path=tmp_path / "spend.json")
    try:
        path = support.write_bench_report(kit, "email", {"batch_size": 30})
    finally:
        kit.close()

    assert path.parent == tmp_path and path.name.startswith("s3_dry_run_email_")
    assert json.loads(path.read_text(encoding="utf-8"))["bench"] == {"name": "email", "batch_size": 30}


def pass_row(label: str, calls_made: int, abandoned: int = 0):
    return {"pass": label, "llm_calls": calls_made, "abandoned_refusals": abandoned}


def test_the_gate_lines_flag_a_long_stage2a_or_cluster_pass_and_any_abandoned_refusal():
    clean = support.gate_summary({"passes": [pass_row("stage1", 20), pass_row("stage2a", 13), pass_row("cluster", 5)]})
    long_pass = support.gate_summary({"passes": [pass_row("cluster", 14)]})
    abandoned = support.gate_summary({"passes": [pass_row("stage1", 3, abandoned=2), pass_row("stage2b", 2)]})
    stopped = support.gate_summary({"passes": [], "spend": {"aborted": True}})

    assert clean[0].startswith("G1 ok") and "most model turns 13" in clean[0] and clean[1].startswith("G2 ok")
    assert long_pass[0].startswith("G1 FAIL")
    assert abandoned[0].startswith("G1: no stage2a or cluster pass") and abandoned[1].startswith("G2 FAIL: 2 ")
    assert any("RUN STOPPED" in line for line in stopped)


# ----------------------------------------------------------------------------------------------- run_s3_kit of each bench (T1)


@pytest.fixture
def kit_files(tmp_path, monkeypatch):
    """The kit's ledger and reports go to the test's directory; `reports(name)` reads what a bench wrote."""
    monkeypatch.setattr(kit_module, "SPEND_LEDGER", tmp_path / "spend.json")

    def reports(name):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(tmp_path.glob(f"s3_dry_run_{name}_*.json"))]

    return reports


def bench_args(**over):
    return SimpleNamespace(user_id=USER, account_id=ACC, budget_usd=30.0, timezone=MADRID, **over)


class Sessions:
    """The sessions collection: one recent session with a user turn and a model turn."""

    def collection(self, name):
        return self

    async def stream(self):
        now = datetime.now(timezone.utc).timestamp()
        yield SimpleNamespace(id=f"{USER}:c1", to_dict=lambda: {
            "owner_id": USER, "last_activity": now,
            "messages": [{"role": "user", "created_at": now - 60, "parts": [{"text": "booked a flight"}]},
                         {"role": "model", "created_at": now - 30, "parts": [{"text": "noted"}]}],
        })


async def test_bench_1_run_s3_kit_runs_one_stage_1_on_the_window_and_reports(store, tmp_path, kit_files):
    agent = bench_agent(store, metered(calls(create("flight booked", expiry=horizon(30)))), answered())

    await bench1.run_s3_kit(handles_for(agent, store, db=Sessions()), 50, bench_args())

    (report,) = kit_files("stage1_classification")
    assert [p["pass"] for p in report["passes"]] == ["stage1"] and report["timezone"] == MADRID
    assert report["bench"]["rule"] == "baseline" and report["bench"]["window_turns"] == 2
    assert [(r["action"], r["status"]) for r in report["writes"]] == [("create", "created")]
    assert (await store.repo.get_active_facts(ACC)) == []                               # nothing written live


@pytest.mark.parametrize("rule,kit,expected", [(None, True, "baseline"), ("baseline", True, "baseline"), (None, False, "both"),
                                              ("new", False, "new"), ("both", False, "both")])
def test_bench_1_rule_resolves_to_baseline_with_the_kit_and_to_its_old_default_without(rule, kit, expected):
    assert bench1.resolve_rule(rule, kit) == expected


@pytest.mark.parametrize("rule", ["new", "both"])
def test_bench_1_refuses_a_candidate_rule_with_the_kit(rule):
    with pytest.raises(ValueError, match="--rule new\\|both"):
        bench1.resolve_rule(rule, True)


@pytest.mark.slow
@pytest.mark.parametrize("script,argv", [
    ("scripts/consolidation/test_stage1_classification_dryrun.py", ["--s3-kit", "--rule", "new"]),
    ("scripts/consolidation/test_stage1_classification_dryrun.py", ["--s3-kit", "--rule", "both"]),
    ("scripts/consolidation/test_directive_review_dryrun.py", ["--s3-kit", "--prompt", "new"]),
    ("scripts/consolidation/test_directive_review_dryrun.py", ["--s3-kit", "--stage1", "new"]),
    ("scripts/email/test_consolidation_dryrun.py", ["--s3-kit", "--category", "travel"]),
])
def test_a_bench_main_refuses_the_flags_the_kit_does_not_run(script, argv):
    code = (
        "import dotenv, runpy, sys\n"
        "dotenv.load_dotenv = lambda *a, **k: None\n"
        f"sys.argv = [{script!r}, '--user-id', 'u', '--account-id', 'a'] + {argv!r}\n"
        f"runpy.run_path({script!r}, run_name='__main__')\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300,
                          env={"PYTHONPATH": str(PROJECT_ROOT), "PATH": "/usr/bin:/bin"})
    assert done.returncode == 2 and "error:" in done.stderr, done.stderr[-800:]


async def test_bench_2_run_s3_kit_runs_one_cluster_pass_per_batch_with_the_batchs_own_facts(store, tmp_path, kit_files):
    texts = ["walks daily and reads at night and cooks on sundays", "plays chess on fridays with an old friend",
             "keeps bees on the farm"]
    ids = [await seed(store, text) for text in texts]
    agent = bench_agent(store, answered())

    await bench2.run_s3_kit(handles_for(agent, store), limit=10, batch_size=2, min_words=3, args=bench_args())

    (report,) = kit_files("cluster_audit")
    assert [p["pass"] for p in report["passes"]] == ["cluster", "cluster"]
    assert [b["input_fact_ids"] for b in report["bench"]["batches"]] == [ids[:2], ids[2:]]
    assert all(b["cluster_size"] >= len(b["input_fact_ids"]) for b in report["bench"]["batches"])


async def test_bench_2_leaves_out_a_seed_an_earlier_batch_retired(store, tmp_path):
    first = await seed(store, "walks daily and reads at night and cooks on sundays")
    second = await seed(store, "plays chess on fridays with an old friend")
    agent, kit = await kit_agent(store, tmp_path, answered())
    entities = [await store.repo.get_fact_by_id(i) for i in (first, second)]
    await agent._fact_management.update_fact(first, {"state": "invalidated"}, ACC, context=kit.write_context("cluster"))

    cluster = await bench2.build_batch_cluster(agent, entities, kit.timezone, USER, ACC)

    assert first not in {e["fact_id"] for e in cluster} and cluster[0]["fact_id"] == second
    assert await bench2.build_batch_cluster(agent, entities[:1], kit.timezone, USER, ACC) == []


async def test_bench_3_run_s3_kit_runs_stage_1_then_a_cluster_pass_over_what_stage_1_wrote(store, tmp_path, kit_files):
    await seed(store, "walks daily and reads at night and cooks on sundays")
    agent = bench_agent(store, metered(calls(create("walks daily", expiry=EXPIRY_NONE))), answered(), answered())

    await bench3.run_s3_kit(handles_for(agent, store), limit=5, min_words=3, args=bench_args())

    (report,) = kit_files("decomposition")
    assert [p["pass"] for p in report["passes"]] == ["stage1", "cluster"]
    assert report["bench"]["stage2"]["cluster_size"] >= 1 and report["bench"]["facts_analyzed"] == 1
    assert [(r["pass"], r["action"]) for r in report["writes"]] == [("stage1", "create")]


async def test_bench_3_has_no_stage_2_step_when_stage_1_wrote_nothing(store, tmp_path, kit_files):
    await seed(store, "walks daily and reads at night and cooks on sundays")
    agent = bench_agent(store, answered())

    await bench3.run_s3_kit(handles_for(agent, store), limit=5, min_words=3, args=bench_args())

    (report,) = kit_files("decomposition")
    assert [p["pass"] for p in report["passes"]] == ["stage1"] and report["bench"]["stage2"] is None


async def test_bench_4_run_s3_kit_starts_every_run_from_the_live_rulebook(store, tmp_path, kit_files):
    first = await seed(store, "always answer in English", domain="agent_directive")
    second = await seed(store, "never use tables", domain="agent_directive", context_priority="high")
    agent = bench_agent(store, metered(calls(invalidate(first))), answered(), metered(calls(invalidate(first))), answered())

    await bench4.run_s3_kit(handles_for(agent, store), runs=2, provider=None, tier="performance", args=bench_args())

    (report,) = kit_files("directive_review")
    assert [p["pass"] for p in report["passes"]] == ["stage2b", "stage2b"]
    runs = report["bench"]["runs"]
    assert [r["verdicts"][first] for r in runs] == ["INVALIDATED", "INVALIDATED"]          # run 2 saw the rule run 1 closed
    assert [r["verdicts"][second] for r in runs] == ["UNTOUCHED", "UNTOUCHED"]
    assert [(r["pass"], r["actor"], r["status"]) for r in report["writes"]] == [("stage2b", "stage2b", "updated")] * 2
    assert (await store.repo.get_fact_by_id(first)).state is FactState.CURRENT


async def test_bench_4_says_so_and_runs_nothing_when_the_rulebook_is_empty(store, tmp_path, kit_files):
    agent = bench_agent(store, answered())

    await bench4.run_s3_kit(handles_for(agent, store), runs=2, provider=None, tier="performance", args=bench_args())

    (report,) = kit_files("directive_review")
    assert report["passes"] == [] and report["spend"]["calls"] == 0


async def test_bench_5_run_s3_kit_runs_one_email_batch_and_marks_nothing(tmp_path, kit_files):
    store, live_emails = email_store()
    await live_emails.save_batch([email("e1", datetime(2026, 10, 10, 9, 0), datetime(2026, 10, 13, 1)),
                                  email("e2", datetime(2026, 10, 12, 9, 0), datetime(2026, 10, 13, 2))])
    agent = bench_agent(store, metered(calls(create("flight booked", expiry=horizon(30)))), answered(), email_repo=live_emails)

    await bench5.run_s3_kit(handles_for(agent, store), 1, bench_args())            # a batch of one, two emails waiting

    (report,) = kit_files("email")
    assert [p["pass"] for p in report["passes"]] == ["email"]
    assert report["bench"]["result"]["batches_processed"] == 1 and report["email_writes"] == {"mark_consolidated": 1}
    assert [e.email_id for e in await live_emails.get_unconsolidated_batch(USER)] == ["e1", "e2"]


async def test_bench_5_stops_before_the_kit_when_there_is_no_unconsolidated_email(tmp_path, kit_files):
    store, live_emails = email_store()
    agent = bench_agent(store, answered(), email_repo=live_emails)

    await bench5.run_s3_kit(handles_for(agent, store), 30, bench_args())

    assert kit_files("email") == [] and not (tmp_path / "spend.json").exists()      # nothing run, nothing spent
