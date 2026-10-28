"""
The drive specialist eval on real LLM calls (DRIVE_SPECIALIST_RFC §12, §15): the production Smart and the production
drive specialist answer the 26 cases of tests/fixtures/drive_eval/gold_specialist_ab.json — content and value
questions, finds, a find then "open the second", transliteration, leads in results and in opened text, versions, a
batch move, a vague delete, a composed text to save, the deleted-files search, a transcript, a recording link, a
photo, and three negatives — on the G3 fixture drive and its cached index. OWNER-run: it spends money; `--max-usd`
is a hard stop.

The same isolation as G3 (`smart_dry_run`): the class-level DryRunDriveOperations stubs every drive write before any
agent exists; the specialist and FileManagement share one fixture DriveOperations with no notification service;
memory and mail answer empty on `isolate` cases; skill drafts, memory proposals and the workspace run dry; a call to a
removed drive intent gets today's unknown-intent refusal and is counted. No Firestore write: billing is off on every
agent, sessions are in memory, Smart's background history summary is off; reads (prompts, user config, memory on the
negative cases) are G3's reads.

The "before" is a report of today's design on the same cases (the spike's stage 1, `--compare <report.json>
--compare-design today`); this run is the "after". The latency bar (§12): p50 of the case wall time after ≤ p50
before + 25 %.

Per case: correct against the gold, Smart↔specialist round trips, Smart / specialist / other LLM calls, wall time,
tokens (input / cached / output) by group, $ by model, every drive operation (op log) and every commission (query,
status). The report goes to the gitignored scripts/memory/drive_g3/ as JSON and markdown.

On a machine with a local proxy (127.0.0.1:8888), set NO_PROXY='*' or the provider SDKs fail TLS.

    NO_PROXY='*' venv/bin/python -m scripts.validation.drive_eval.specialist_ab --smart-model gpt-6.1-sol \\
        --max-usd 3 --compare scripts/memory/drive_g3/specialist_ab_<stage1>.json --compare-design today
    NO_PROXY='*' venv/bin/python -m scripts.validation.drive_eval.specialist_ab --replay-commissions <report.json> \\
        --max-usd 1
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.agents.base_agent import BaseAgent
from src.domain.llm import Message, MessagePart
from src.domain.session import Session
from src.domain.user_drive import parse_drive_ref
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import DriveOperations
from src.ports.session_store import SessionStore

from .cost_meter import UNPRICED_USD_PER_M, CostCapReached, Meter, price   # noqa: F401 — re-exported for the replay and the tests
from .fixture_drive import item_id_for
from .smart_dry_run import (
    REMOVED_DRIVE_INTENTS, DryRunDispatch, answer_ok, goes_to_drive, interactive_turn_clock, wall_p50)

KINDS = frozenset({"content", "value_file_word", "value_no_file_word", "find", "find_then_open", "translit",
                   "lead_in_results", "lead_in_opened_text", "versions", "batch_move", "vague_change", "save_text",
                   "deleted_search", "transcript", "recording_link", "photo", "negative_memory", "negative_web",
                   "negative_reminder"})
GOLD_KEYS = frozenset({"drive", "answer_must_include", "must_open", "must_not_open", "opened_any",
                       "ops_must_include", "ops_forbidden", "intents_must_include"})
SMART_TYPE, SPECIALIST_TYPE = "smart_response", "drive"
LATENCY_BAR = 1.25   # p50 after ≤ p50 before × 1.25 (§12)

_REPO = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO / "tests" / "fixtures" / "drive_eval"
_OUT = _REPO / "scripts" / "memory" / "drive_g3"
EXIT_OK, EXIT_SETUP, EXIT_STOPPED = 0, 2, 3


class AbError(RuntimeError):
    """No run: a malformed gold file or a setup problem."""


# -- gold ------------------------------------------------------------------------------------------------------------

def _op_params(op: str) -> set:
    """The arguments a drive operation log entry names for `op` (dry_run_drive.named_args): every parameter of the
    DriveOperations method but self, context, user_id and a mutation's wait."""
    params = inspect.signature(getattr(DriveOperations, op)).parameters
    return set(params) - {"self", "context", "user_id", "wait_s"}


def validate_ab_gold(gold: Sequence[Mapping[str, Any]], paths: Optional[Sequence[str]] = None) -> None:
    """Raise AbError listing every problem. An op in the gold is a DriveOperations method; its args are that method's
    keyword parameters, so a renamed operation fails here, not silently in a paid run."""
    problems: List[str] = []
    ids = [c.get("id") for c in gold]
    problems += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    known = set(paths or [])
    ops = DriveOperations.READ_METHODS | DriveOperations.MUTATION_METHODS
    for case in gold:
        cid = case.get("id")
        turns = case.get("turns")
        if not (isinstance(turns, list) and turns and all(isinstance(x, str) and x.strip() for x in turns)):
            problems.append(f"{cid}: turns must be a non-empty list of texts")
        if case.get("kind") not in KINDS:
            problems.append(f"{cid}: unknown kind {case.get('kind')!r}")
        g = case.get("gold")
        if not isinstance(g, dict) or "drive" not in g:
            problems.append(f"{cid}: gold must be an object with 'drive'")
            continue
        extra = set(g) - GOLD_KEYS
        if extra:
            problems.append(f"{cid}: unknown gold keys {sorted(extra)}")
        if g.get("must_open") and g.get("must_not_open"):
            problems.append(f"{cid}: must_open and must_not_open contradict each other")
        if not g["drive"] and any(g.get(k) for k in ("must_open", "opened_any", "ops_must_include")):
            problems.append(f"{cid}: a no-drive case cannot require drive operations")
        groups = g.get("answer_must_include")
        if groups is not None and not (isinstance(groups, list) and all(
                isinstance(x, list) and x and all(isinstance(w, str) and w.strip() for w in x) for x in groups)):
            problems.append(f"{cid}: answer_must_include must be a list of non-empty lists of words")
        for rule in g.get("ops_must_include") or []:
            if rule.get("op") not in ops:
                problems.append(f"{cid}: unknown drive operation {rule.get('op')!r}")
            elif set(rule.get("args") or {}) - _op_params(rule["op"]):
                problems.append(f"{cid}: {rule['op']} has no parameter {sorted(set(rule['args']) - _op_params(rule['op']))}")
        problems += [f"{cid}: unknown drive operation {o!r}" for o in g.get("ops_forbidden") or [] if o not in ops]
        problems += [f"{cid}: {i!r} is a removed drive intent" for i in g.get("intents_must_include") or []
                     if i in REMOVED_DRIVE_INTENTS]
        if known:
            problems += [f"{cid}: opened_any path not on the fixture drive: {p}" for p in g.get("opened_any") or []
                         if p not in known]
        if not (g.get("answer_must_include") or g.get("ops_must_include") or g.get("ops_forbidden")
                or g.get("intents_must_include") or g.get("opened_any") or not g["drive"]):
            problems.append(f"{cid}: gold checks nothing")
    if problems:
        raise AbError("malformed specialist eval gold: " + "; ".join(problems))


# -- what ran ----------------------------------------------------------------------------------------------------------

class InMemorySessions(SessionStore):
    """Sessions live in this process only (multi-turn cases); nothing reaches Firestore."""

    def __init__(self) -> None:
        self.sessions: Dict[str, Session] = {}

    async def load_session(self, session_id: str) -> Session:
        return self.sessions.get(session_id) or Session(session_id=session_id)

    async def save_session(self, session_id: str, state: Session) -> None:
        self.sessions[session_id] = state

    async def append_message(self, session_id: str, message: Message, owner_id: Optional[str] = None) -> None:
        self.sessions.setdefault(session_id, Session(session_id=session_id)).add_message(message)

    async def append_messages_batch(self, session_id: str, messages: list, owner_id: Optional[str] = None, *,
                                    raise_on_error: bool = False) -> None:
        for message in messages:
            await self.append_message(session_id, message, owner_id)

    async def get_latest_session_id(self, owner_id: str) -> Optional[str]:
        return None


def disable_billing_writes() -> Callable[[], None]:
    """No agent writes usage to Firestore (eager or lazily built later): the one Firestore write on an agent's path."""
    original = BaseAgent._flush_billing

    async def no_flush(agent: BaseAgent) -> None:
        return None

    BaseAgent._flush_billing = no_flush   # type: ignore[method-assign]

    def restore() -> None:
        BaseAgent._flush_billing = original   # type: ignore[method-assign]
    return restore


# -- checks and metrics -----------------------------------------------------------------------------------------------

def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"))


def _args_match(want: Mapping[str, Any], got: Mapping[str, Any]) -> bool:
    for key, expected in want.items():
        value = got.get(key)
        if isinstance(expected, bool):
            if _truthy(value) != expected:
                return False
        elif str(expected).casefold() not in str(value or "").casefold():
            return False
    return True


def drive_opens(ops: Sequence[Mapping[str, Any]]) -> List[str]:
    """Item ids of the drive files opened for their content (a recording link is not an open)."""
    out: List[str] = []
    for o in ops:
        if o["op"] == "open" and not _truthy((o.get("args") or {}).get("original")):
            item = parse_drive_ref(str((o.get("args") or {}).get("ref", "")))   # DriveOperations.open(…, ref)
            if item:
                out.append(item)
    return out


def check_case(case: Mapping[str, Any], answer: str, ops: Sequence[Mapping[str, Any]],
               calls: Sequence[Mapping[str, Any]]) -> List[str]:
    """The gold's failed checks (empty = correct). `ops` is the drive operation log, `calls` Smart's delegations."""
    g = case["gold"]
    failures: List[str] = []
    drive_calls = [c for c in calls if goes_to_drive(c["intent"], c.get("context") or {})
                   or c["intent"] in REMOVED_DRIVE_INTENTS]
    used = bool(ops) or bool(drive_calls)
    if g["drive"] and not used:
        failures.append("the drive was not used")
    if not g["drive"] and used:
        failures.append(f"the drive was used: {[c['intent'] for c in drive_calls] or [o['op'] for o in ops]}")
    groups = g.get("answer_must_include")
    spaced = answer.replace(" ", " ").replace(" ", " ")   # a model's thin and no-break spaces
    if groups and not (answer_ok(answer, groups) or answer_ok(spaced, groups)):
        missing = [grp for grp in groups if not any(w.casefold() in spaced.casefold() for w in grp)]
        failures.append(f"answer lacks {missing}")
    opened = drive_opens(ops)
    if g.get("must_open") and not opened:
        failures.append("no file was opened")
    if g.get("must_not_open") and opened:
        failures.append(f"{len(opened)} file(s) opened on a find")
    if g.get("opened_any") and not any(item_id_for(p) in opened for p in g["opened_any"]):
        failures.append(f"none of {g['opened_any']} was opened")
    for rule in g.get("ops_must_include") or []:
        hits = [o for o in ops if o["op"] == rule["op"] and _args_match(rule.get("args") or {}, o.get("args") or {})]
        if len(hits) < int(rule.get("min", 1)):
            failures.append(f"{rule['op']} {rule.get('args') or {}}: {len(hits)} of at least {rule.get('min', 1)}")
    for op in g.get("ops_forbidden") or []:
        if any(o["op"] == op for o in ops):
            failures.append(f"forbidden {op} ran")
    for intent in g.get("intents_must_include") or []:
        if not any(c["intent"] == intent for c in calls):
            failures.append(f"{intent} was not called")
    return failures


def _group(agent_type: str) -> str:
    return "smart" if agent_type == SMART_TYPE else "specialist" if agent_type == SPECIALIST_TYPE else "other"


def usage_summary(llm_calls: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    tokens = {g: {"calls": 0, "input": 0, "cached": 0, "output": 0, "usd": 0.0}
              for g in ("smart", "specialist", "other")}
    by_model: Dict[str, float] = {}
    unpriced: set = set()
    for c in llm_calls:
        row = tokens[_group(c["agent_type"])]
        row["calls"] += 1
        for k in ("input", "cached", "output"):
            row[k] += c[k]
        row["usd"] = round(row["usd"] + c["usd"], 6)
        by_model[c["model"]] = round(by_model.get(c["model"], 0.0) + c["usd"], 6)
        if not c["priced"]:
            unpriced.add(c["model"])
    return {"tokens": tokens, "usd_by_model": by_model, "usd": round(sum(by_model.values()), 6),
            "unpriced_models": sorted(unpriced)}


def history_pair(text: str, answer: str, metadata: Mapping[str, Any]) -> List[Message]:
    """The session pair ConversationHandler writes: the answer, with every `*_context` block in full_text."""
    full = answer
    for key, value in (metadata or {}).items():
        if key.endswith("_context") and value:
            full += "\n\n" + json.dumps({key: value}, ensure_ascii=False, separators=(",", ":"))
    now = time.time()
    return [Message(role="user", parts=[MessagePart(text=text)], created_at=now),
            Message(role="model", parts=[MessagePart(text=answer, full_text=full)], created_at=now + 1)]


def answer_text(response: Any) -> str:
    result = getattr(response, "result", None)
    if result is None:
        return f"[FAILED] {getattr(response, 'error', '')}"
    return str(getattr(result, "text", result))


async def run_case(case: Mapping[str, Any], *, smart: Any, dispatcher: DryRunDispatch, drive_log: Any, meter: Meter,
                   sessions: InMemorySessions, user_id: str, account_id: str, repeat: int = 0,
                   request_context: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """One case (all its turns): the row of the report."""
    from src.domain.agent import AgentIntent, AgentMessage
    from src.domain.turn_clock import CURRENT_TURN_CLOCK

    drive_log.clear()
    dispatcher.calls.clear()
    dispatcher.isolate = bool(case.get("isolate"))
    session_id = f"{user_id}:drive_specialist_eval:{case['id']}:{repeat}"
    mark = meter.mark()
    started = time.monotonic()
    answer, error = "", ""
    turn_rows: List[Dict[str, Any]] = []
    for text in case["turns"]:
        turn_mark, turn_calls = meter.mark(), len(dispatcher.calls)
        msg = AgentMessage.create(
            sender="drive_specialist_eval", recipient=smart.agent_id, intent=AgentIntent.QUERY, payload={"text": text},
            context={"user_id": user_id, "account_id": account_id, "session_id": session_id,
                     "current_message_parts": [MessagePart(text=text)], "interactive_delivery": True})
        token = CURRENT_TURN_CLOCK.set(interactive_turn_clock())
        turn_started = time.monotonic()
        try:
            if request_context is not None:
                async with request_context():
                    response = await smart.execute(msg)
            else:
                response = await smart.execute(msg)
        finally:
            CURRENT_TURN_CLOCK.reset(token)
        answer = answer_text(response)
        if meter.capped:
            error = "cost cap reached inside the case"
        turn_rows.append({"text": text, "answer": answer[:4000], "s": round(time.monotonic() - turn_started, 2),
                          "smart_calls": sum(1 for c in meter.since(turn_mark) if c["agent_type"] == SMART_TYPE),
                          "delegations": [c["intent"] for c in dispatcher.calls[turn_calls:]]})
        await sessions.append_messages_batch(session_id, history_pair(text, answer, getattr(response, "metadata", {})))
        if error:
            break
    wall = round(time.monotonic() - started, 2)
    ops = list(drive_log.entries)
    calls = list(dispatcher.calls)
    failures = check_case(case, answer, ops, calls) if not error else [error]
    usage = usage_summary(meter.since(mark))
    commissions = [c for c in calls if c["intent"] == Intent.WORK_WITH_DRIVE_FILES]
    return {
        "id": case["id"], "kind": case["kind"], "repeat": repeat, "correct": not failures, "failures": failures,
        "wall_s": wall, "round_trips": len(commissions),
        "drive_statuses": [c.get("drive_status", "") for c in commissions],
        "removed_intent_calls": sum(1 for c in calls if c["route"] == "removed"),
        "smart_llm_calls": usage["tokens"]["smart"]["calls"],
        "specialist_llm_calls": usage["tokens"]["specialist"]["calls"],
        "other_llm_calls": usage["tokens"]["other"]["calls"],
        **usage, "models": sorted({c["model"] for c in meter.since(mark)}),
        "answer": answer[:4000], "turns": turn_rows, "calls": calls, "ops": ops,
    }


def estimate_next(rows: Sequence[Mapping[str, Any]]) -> float:
    return max((float(r.get("usd", 0.0)) for r in rows), default=0.0)


async def run_all(cases: Sequence[Mapping[str, Any]], repeats: int, *, run_one: Callable[..., Any],
                  meter: Meter) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Every case `repeats` times; (rows, why it stopped early or None). No case starts once spend so far plus the
    dearest case so far would pass the cap; a case cut by the cap inside (Meter.check) ends the run."""
    rows: List[Dict[str, Any]] = []
    for repeat in range(repeats):
        for case in cases:
            if meter.spent + estimate_next(rows) > meter.max_usd:
                reason = (f"${meter.spent:.4f} spent + ${estimate_next(rows):.4f} for the next case would pass "
                          f"--max-usd {meter.max_usd}")
                print(f"stop: {reason}")
                return rows, reason
            row = await run_one(case, repeat=repeat)
            rows.append(row)
            print(f"{'+' if row['correct'] else '!'} {case['id']:<32} trips={row['round_trips']} "
                  f"smart={row['smart_llm_calls']} spec={row['specialist_llm_calls']} {row['wall_s']}s "
                  f"${row['usd']:.4f}" + ("" if row["correct"] else f"  {row['failures']}"))
            if meter.capped:
                return rows, f"the cost cap was reached inside {case['id']}"
    return rows, None


# -- before / after ----------------------------------------------------------------------------------------------------

def summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {"cases": len(rows), "correct": sum(1 for r in rows if r.get("correct")),
            "wall_s": round(sum(float(r.get("wall_s", 0.0)) for r in rows), 1), "wall_p50_s": wall_p50(rows),
            "smart_llm_calls": sum(int(r.get("smart_llm_calls", 0)) for r in rows),
            "specialist_llm_calls": sum(int(r.get("specialist_llm_calls", 0)) for r in rows),
            "usd": round(sum(float(r.get("usd", 0.0)) for r in rows), 4)}


def compare(before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Before and after on the cases both ran: the totals, the cases whose verdict changed and the latency bar."""
    ids = {r["id"] for r in before} & {r["id"] for r in after}
    b = [r for r in before if r["id"] in ids]
    a = [r for r in after if r["id"] in ids]
    verdict = {r["id"]: bool(r.get("correct")) for r in b}
    changed = sorted({(r["id"], verdict[r["id"]], bool(r.get("correct"))) for r in a
                      if verdict.get(r["id"]) != bool(r.get("correct"))})
    sb, sa = summary(b), summary(a)
    return {"before": sb, "after": sa, "changed": [{"id": i, "before": x, "after": y} for i, x, y in changed],
            "latency_ok": sa["wall_p50_s"] <= sb["wall_p50_s"] * LATENCY_BAR + 1e-9}


def load_compare_rows(path: Path, design: str = "") -> List[Dict[str, Any]]:
    """A report's rows; the spike's reports hold several designs (`design`), this eval's one."""
    rows = json.loads(path.read_text(encoding="utf-8")).get("rows", [])
    return [r for r in rows if not design or r.get("design") == design]


# -- report ----------------------------------------------------------------------------------------------------------

def _tok(row: Mapping[str, Any], group: str) -> str:
    t = row["tokens"][group]
    return f"{t['input']}/{t['cached']}/{t['output']}"


def markdown(rows: Sequence[Mapping[str, Any]], meta: Mapping[str, Any],
             comparison: Optional[Mapping[str, Any]] = None) -> str:
    lines = [f"# Drive specialist eval — {meta['stamp']}", "",
             f"Smart model: {meta['smart_model']} · spent ${meta['spent']:.4f} of ${meta['max_usd']}"
             + (f" · **stopped early: {meta['stopped']}**" if meta.get("stopped") else ""), "",
             "| case | ok | round trips | statuses | Smart calls | spec calls | other calls | wall s "
             "| Smart in/cached/out | spec in/cached/out | $ |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {'yes' if r['correct'] else 'NO'} | {r['round_trips']} "
                     f"| {','.join(r['drive_statuses']) or '-'} | {r['smart_llm_calls']} | {r['specialist_llm_calls']} "
                     f"| {r['other_llm_calls']} | {r['wall_s']} | {_tok(r, 'smart')} | {_tok(r, 'specialist')} "
                     f"| {r['usd']:.4f} |")
    s = summary(rows)
    lines += ["", f"Total: {s['correct']}/{s['cases']} correct, wall {s['wall_s']} s (p50 {s['wall_p50_s']} s), "
                  f"Smart calls {s['smart_llm_calls']}, specialist calls {s['specialist_llm_calls']}, ${s['usd']:.4f}"]
    removed = sum(r["removed_intent_calls"] for r in rows)
    if removed:
        lines.append(f"**Calls to removed drive intents: {removed}**")
    failing = [r for r in rows if not r["correct"]]
    if failing:
        lines += ["", "Failures:"] + [f"- {r['id']}: {'; '.join(r['failures'])}" for r in failing]
    models: Dict[str, float] = {}
    for r in rows:
        for m, usd in r["usd_by_model"].items():
            models[m] = models.get(m, 0.0) + usd
    lines += ["", "$ by model: " + ", ".join(f"{m} ${v:.4f}" for m, v in sorted(models.items()))]
    unpriced = sorted({m for r in rows for m in r["unpriced_models"]})
    if unpriced:
        lines.append(f"Unpriced in billing.py (charged at the fallback rate for the stop): {', '.join(unpriced)}")
    if comparison is not None:
        b, a = comparison["before"], comparison["after"]
        lines += ["", "| | cases | correct | wall s | wall p50 s | Smart calls | spec calls | $ |",
                  "|---|---|---|---|---|---|---|---|"]
        for label, x in (("before", b), ("after", a)):
            lines.append(f"| {label} | {x['cases']} | {x['correct']} | {x['wall_s']} | {x['wall_p50_s']} "
                         f"| {x['smart_llm_calls']} | {x['specialist_llm_calls']} | {x['usd']:.4f} |")
        lines.append(f"Latency bar (p50 after ≤ p50 before × {LATENCY_BAR}): "
                     + ("met" if comparison["latency_ok"] else "**missed**"))
        lines += [f"- {c['id']}: {'ok' if c['before'] else 'NO'} → {'ok' if c['after'] else 'NO'}"
                  for c in comparison["changed"]]
    return "\n".join(lines) + "\n"


def write_report(rows: Sequence[Mapping[str, Any]], meta: Mapping[str, Any], comparison: Optional[Mapping[str, Any]]
                 = None, out_dir: Path = _OUT, stem: str = "specialist_ab") -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{stem}_{meta['stamp']}"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps({"meta": dict(meta), "rows": list(rows), "comparison": comparison},
                                    ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    md_path.write_text(markdown(rows, meta, comparison), encoding="utf-8")
    return json_path, md_path


# -- the run -----------------------------------------------------------------------------------------------------------

def _args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.specialist_ab")
    parser.add_argument("--smart-model", default="", help="pin Smart's model (default: the owner's config)")
    parser.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    parser.add_argument("--max-usd", type=float, required=True, help="hard stop on the estimated spend")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--gold", type=Path, default=_FIXTURES / "gold_specialist_ab.json")
    parser.add_argument("--tree", type=Path, default=_FIXTURES / "tree.json")
    parser.add_argument("--tree-g3", type=Path, default=_FIXTURES / "tree_g3.json")
    parser.add_argument("--tree-v2", type=Path, default=_FIXTURES / "tree_v2.json")
    parser.add_argument("--snapshot", type=Path, default=_FIXTURES / "enrichment.json")
    parser.add_argument("--compare", type=Path, default=None, help="a report JSON to compare with (the 'before')")
    parser.add_argument("--compare-design", default="", help="that report's design to compare (a spike report: today)")
    parser.add_argument("--replay-commissions", type=Path, default=None,
                        help="a report JSON: replay its commissions on the specialist only, no Smart")
    parser.add_argument("--replay-design", default="", help="the design whose commissions to replay (spike reports)")
    parser.add_argument("--replay-timezone", default="", help="override the user timezone in the specialist's state")
    parser.add_argument("--rebuild-index", action="store_true",
                        help="build the fixture index even when scripts/memory/drive_g3/cache/ holds a matching one")
    return parser.parse_args(argv)


def select_cases(gold: Sequence[Mapping[str, Any]], ids: str) -> List[Mapping[str, Any]]:
    if not ids.strip():
        return list(gold)
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    known = {c["id"]: c for c in gold}
    missing = [i for i in wanted if i not in known]
    if missing:
        raise AbError(f"unknown case ids: {missing}")
    return [known[i] for i in wanted]


_PROVIDER_BY_PREFIX = {"claude": "claude", "gpt": "openai", "o1": "openai", "o3": "openai", "gemini": "gemini",
                       "grok": "grok"}


def provider_of(model: str) -> str:
    name = next((v for k, v in _PROVIDER_BY_PREFIX.items() if model.startswith(k)), None)
    if name is None:
        raise AbError(f"no provider known for model {model!r}")
    return name


def pin_smart_model(smart: Any, model: str, registry: Any) -> None:
    """ab_agent_models.py's pin: every Smart request runs on `model`, through that model's adapter, no failover."""
    import copy

    original = smart._resolve_effective
    provider = provider_of(model)

    def pinned(message: Any) -> Any:
        eff = original(message)
        ctx = copy.copy(eff.ctx)
        ctx.model_name = model
        if provider != ctx.provider_name:
            ctx.provider = registry.get(provider)
            ctx.provider_name = provider
            ctx.capabilities = ctx.provider.CAPABILITIES
        ctx.fallback_provider = ctx.fallback_model_name = ctx.fallback_provider_name = None
        return type(eff)(ctx=ctx, thinking_effort=eff.thinking_effort)

    smart._resolve_effective = pinned


@dataclass
class World:
    """Everything a run needs, built once: the fixture, the patched dispatch, the sessions, the meter."""
    smart: Any
    fixture: Any
    dispatcher: DryRunDispatch
    drive_log: Any
    sessions: InMemorySessions
    meter: Meter
    user_id: str
    account_id: str
    restore: List[Callable[[], None]]


async def build_world(args: argparse.Namespace, gold: Sequence[Mapping[str, Any]]) -> World:
    """Real Smart, Firestore prompts (read only), model keys; the fixture drive and its cached index; every write
    stub installed before the first agent is built."""
    from google.cloud import firestore

    from scripts.validation.ab_agent_models import _READ_ONLY_INTENTS
    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.adapters.firestore_user_repo import FirestoreUserRepository
    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
    from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
    from src.composition.drive_indexing_setup import build_place_lookup
    from src.composition.service_container import ServiceContainer
    from src.composition.user_agent_factory import UserAgentFactory
    from src.config.settings import load_settings
    from src.infrastructure.agent_coordinator import AgentCoordinator
    from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
    from src.infrastructure.agent_registry import AgentRegistry
    from src.infrastructure.delegation_engine import DelegationEngine, ToolResult
    from src.services.drive_search_service import DriveSearchService
    from src.services.dry_run_workspace import DryRunWorkspace

    from .dry_run_drive import DriveOpLog, DryRunDriveOperations
    from .enrichment_snapshot import EnrichmentSnapshot
    from .index_cache import load_or_build
    from .parse_cache import ParseCache
    from .runner import EvalClock, _EnabledUsers
    from .search_v2_eval import index_v2
    from .smart_dry_run import DryRunSkills, RecordingMemoryFacts, annotate_drive_read, g3_drive
    from .specialist_fixture import build_drive_fixture

    restore: List[Callable[[], None]] = [disable_billing_writes()]
    drive_log = DriveOpLog()
    dry_run = DryRunDriveOperations(drive_log, annotate=annotate_drive_read)
    dry_run.install()
    restore.append(dry_run.uninstall)
    drive = g3_drive(args.tree, args.tree_g3, args.tree_v2)
    validate_ab_gold(gold, drive.paths())
    user_id, account_id = os.environ["DEV_USER_ID"], os.environ["DEV_ACCOUNT_ID"]
    embeddings = GeminiEmbeddingAdapter(api_key=os.environ["GEMINI_API_KEY"])
    clock = EvalClock()

    async def build_index() -> Any:
        return await index_v2(drive, EnrichmentSnapshot.load(args.snapshot), embeddings,
                              GeminiEmbeddingAdapter.EMBEDDING_SPACE, build_place_lookup(),
                              ParseCache(SubprocessContentExtractor(), _REPO / "scripts" / "memory"
                                         / "drive_eval_parse_cache"), clock)

    repo, facts = await load_or_build(build_index, trees=(args.tree, args.tree_g3, args.tree_v2),
                                      snapshot=args.snapshot, drive=drive,
                                      embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE, clock=clock,
                                      rebuild=args.rebuild_index)
    if facts["misses"]:
        raise AbError(f"the enrichment snapshot misses {facts['misses']}: re-record it (dump_enrichment.py)")
    search = DriveSearchService(repo, embeddings, _EnabledUsers(), drive, clock=clock,
                                embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE)
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    settings = load_settings()
    env_config = settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    container = ServiceContainer(config=settings, db_client=db, env_config=env_config, account_repo=account_repo)
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    factory = UserAgentFactory(config=settings, env_config=env_config, coordinator=AgentCoordinator(registry=registry),
                               user_repo=user_repo, account_repo=account_repo, **container.agent_services())
    agents = await factory.ensure_agents_for_user(user_id)
    smart = agents["smart_agent"]
    if smart.skill_service is not None:
        smart.skill_service = DryRunSkills(smart.skill_service)
    if smart.workspace_service is not None:
        smart.workspace_service = DryRunWorkspace(smart.workspace_service)
    smart.memory_fact_service = RecordingMemoryFacts()
    smart.history_summary_service = None   # its background summary call is not part of the turn
    sessions = InMemorySessions()
    smart.session_store = sessions
    if args.smart_model:
        pin_smart_model(smart, args.smart_model, container.registry)
    user_context = agents["_user_context"]
    config = user_context.user_profile.config
    if args.replay_timezone:
        config = config.model_copy(update={"timezone": args.replay_timezone})
    fixture = build_drive_fixture(user_id, drive, search, user_config=config,
                                  prompt_builder=user_context.prompt_builder, context_builder=container.context_builder)
    dispatcher = DryRunDispatch(DelegationEngine.dispatch, fixture.engine, frozenset(_READ_ONLY_INTENTS))
    original_dispatch = DelegationEngine.dispatch

    async def guarded(self: Any, tool_call: Any, context: Dict[str, Any], *a: Any, **kw: Any) -> ToolResult:
        return await dispatcher(self, tool_call, context, *a, **kw)

    DelegationEngine.dispatch = guarded   # type: ignore[method-assign]

    def undo_dispatch() -> None:
        DelegationEngine.dispatch = original_dispatch   # type: ignore[method-assign]
    restore.append(undo_dispatch)
    meter = Meter(max_usd=args.max_usd)
    restore.append(meter.install())
    return World(smart, fixture, dispatcher, drive_log, sessions, meter, user_id, account_id, restore)


async def _run(args: argparse.Namespace) -> int:
    from src.domain.request_context import RequestContext

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    cases = select_cases(gold, args.cases)
    world = await build_world(args, gold)
    try:
        async def one(case: Mapping[str, Any], *, repeat: int) -> Dict[str, Any]:
            return await run_case(case, smart=world.smart, dispatcher=world.dispatcher, drive_log=world.drive_log,
                                  meter=world.meter, sessions=world.sessions, user_id=world.user_id,
                                  account_id=world.account_id, repeat=repeat,
                                  request_context=lambda: RequestContext(user_id=world.user_id,
                                                                         account_id=world.account_id))
        rows, stopped = await run_all(cases, args.repeats, run_one=one, meter=world.meter)
    finally:
        for undo in reversed(world.restore):
            undo()
    comparison = compare(load_compare_rows(args.compare, args.compare_design), rows) if args.compare else None
    meta = {"stamp": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
            "smart_model": args.smart_model or "owner config", "max_usd": args.max_usd,
            "spent": round(world.meter.spent, 6), "stopped": stopped, "cases": [c["id"] for c in cases],
            "repeats": args.repeats}
    json_path, md_path = write_report(rows, meta, comparison)
    print(markdown(rows, meta, comparison))
    print(f"report: {json_path}\n        {md_path}")
    return EXIT_STOPPED if stopped else EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
    try:
        args = _args(argv)
        if args.replay_commissions is not None:
            from .specialist_replay import run_replay
            return asyncio.run(run_replay(args))
        return asyncio.run(_run(args))
    except AbError as exc:
        print(f"drive specialist eval: no run — {exc}", file=sys.stderr)
        return EXIT_SETUP


if __name__ == "__main__":
    sys.exit(main())
