"""
The drive specialist alone on recorded commissions (`specialist_ab --replay-commissions`, DRIVE_SPECIALIST_RFC §15.3):
compare specialist prompts or models without paying for Smart.

Reads every `work_with_drive_files` commission an earlier report recorded — this eval's (`specialist_ab_*.json`) or
the spike's (pick its design with `--replay-design specialist`) — with the commission's query, its context and the
user's own message of that turn, and runs ONLY the drive specialist on each, through the coordinator path
(`handle_delegation`, which adds the timestamp prefix), on the same fixture drive and cached index, with the same
write stubs. Smart is built (its user context gives the specialist its prompt builder) but never called.

Per commission: the status, the drive operations, whether the verbatim content delivered to Smart holds the case's
gold (the checks that apply to delivered content: values, opens, ops), the specialist's LLM calls, wall time, tokens
and $. `--max-usd` applies as in the eval.

Limits: a report stores a commission's query cut at 2000 characters (flagged `query_truncated`); a value inside an
attached image or PDF is not in the delivered text (`vision_files` says how many were attached).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.domain.llm import MessagePart
from src.infrastructure.agent_manifest import Intent

from .fixture_drive import item_id_for
from .smart_dry_run import answer_ok
from .specialist_ab import (
    EXIT_OK, EXIT_STOPPED, AbError, Meter, World, _args_match, afc_failures, afc_metrics, build_world, drive_opens,
    estimate_next, origin_context, usage_summary, write_report)

QUERY_CUT = 2000   # the eval stores calls[].query cut at this length
_ROUTES = ("specialist", "fixture")   # the spike's route for a commission, and this eval's


def commissions_from_report(report: Mapping[str, Any], design: str = "") -> List[Dict[str, Any]]:
    """Every commission Smart sent to the specialist, in run order, with the user's message of its turn. A spike
    report holds several designs: `design` picks one; this eval's reports hold one and need none."""
    out: List[Dict[str, Any]] = []
    for row in report.get("rows", []):
        if design and row.get("design") != design:
            continue
        turns = row.get("turns") or []
        turn_of: List[int] = []
        for index, turn in enumerate(turns):
            turn_of.extend([index] * len(turn.get("delegations") or []))
        n = 0
        for position, call in enumerate(row.get("calls") or []):
            if call.get("intent") != Intent.WORK_WITH_DRIVE_FILES or call.get("route") not in _ROUTES:
                continue
            n += 1
            turn = turn_of[position] if position < len(turn_of) else max(len(turns) - 1, 0)
            query = str(call.get("query") or "")
            out.append({"case_id": row["id"], "repeat": row.get("repeat", 0), "n": n, "turn": turn,
                        "query": query, "query_truncated": len(query) >= QUERY_CUT,
                        "params": dict(call.get("context") or {}), "origin": str(row.get("origin") or ""),
                        "user_message": str(turns[turn]["text"]) if turns else ""})
    return out


def commissions_from_gold(gold: Sequence[Mapping[str, Any]], ids: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """The gold's own commissions (`commission` on a case, the method-dictating ones): the user's words are the
    case's first turn, the origin the case's."""
    out: List[Dict[str, Any]] = []
    for case in gold:
        if not case.get("commission") or (ids and case["id"] not in ids):
            continue
        out.append({"case_id": case["id"], "repeat": 0, "n": 1, "turn": 0, "query": str(case["commission"]),
                    "query_truncated": False, "params": {}, "origin": str(case.get("origin") or ""),
                    "user_message": str(case["turns"][0])})
    return out


def content_checks(gold: Mapping[str, Any], text: str, ops: Sequence[Mapping[str, Any]]) -> List[str]:
    """The gold checks that apply to what the specialist delivered: values or names in the content, opens, ops.
    (`drive` and `intents_must_include` are Smart's and are not checked here.)"""
    failures: List[str] = []
    groups = gold.get("answer_must_include")
    spaced = text.replace(" ", " ").replace(" ", " ")
    if groups and not (answer_ok(text, groups) or answer_ok(spaced, groups)):
        missing = [g for g in groups if not any(w.casefold() in spaced.casefold() for w in g)]
        failures.append(f"content lacks {missing}")
    opened = drive_opens(ops)
    if gold.get("must_open") and not opened:
        failures.append("no file was opened")
    if gold.get("must_not_open") and opened:
        failures.append(f"{len(opened)} file(s) opened on a find")
    if gold.get("opened_any") and not any(item_id_for(p) in opened for p in gold["opened_any"]):
        failures.append(f"none of {gold['opened_any']} was opened")
    for rule in gold.get("ops_must_include") or []:
        hits = [o for o in ops if o["op"] == rule["op"] and _args_match(rule.get("args") or {}, o.get("args") or {})]
        if len(hits) < int(rule.get("min", 1)):
            failures.append(f"{rule['op']} {rule.get('args') or {}}: {len(hits)} of at least {rule.get('min', 1)}")
    for op in gold.get("ops_forbidden") or []:
        if any(o["op"] == op for o in ops):
            failures.append(f"forbidden {op} ran")
    return failures


async def replay_one(commission: Mapping[str, Any], gold: Optional[Mapping[str, Any]], world: World,
                     case: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    from src.domain.request_context import RequestContext

    world.drive_log.clear()
    mark = world.meter.mark()
    trace = getattr(world, "trace", None)
    trace_mark = trace.mark() if trace is not None else (0, 0)
    origin = str((case or {}).get("origin") or commission.get("origin") or "chat")
    started = time.monotonic()
    context = {"user_id": world.user_id, "account_id": world.account_id,
               "session_id": f"{world.user_id}:drive_replay:{commission['case_id']}:{commission['n']}",
               "current_message_parts": [MessagePart(text=commission["user_message"])],
               **origin_context(origin), "params": dict(commission["params"])}
    async with RequestContext(user_id=world.user_id, account_id=world.account_id):
        response = await world.fixture.coordinator.handle_delegation(
            intent=Intent.WORK_WITH_DRIVE_FILES, query=commission["query"], context=context,
            calling_agent_id="drive_replay")
    wall = round(time.monotonic() - started, 2)
    text = str(response.result or response.error or "")
    files = (response.metadata or {}).get("file_data")
    files = files if isinstance(files, list) else [files] if files else []
    ops = list(world.drive_log.entries)
    usage = usage_summary(world.meter.since(mark))
    failures = content_checks(gold or {}, text, ops) if gold is not None else ["no gold for this case"]
    afc: Dict[str, Any] = {}
    if case is not None and case.get("family"):
        afc = afc_metrics(ops, text, trace.since(trace_mark) if trace is not None else {},
                          case.get("reference_points") or (), points_in_answer=False)
        failures = failures + afc_failures(case, afc, text, answer_known=False)
    if world.meter.capped:
        failures = ["cost cap reached inside the commission"]
    return {
        **({"family": case["family"], "origin": origin, "afc": afc} if afc else {}),
        "id": f"{commission['case_id']}#{commission['n']}", "case_id": commission["case_id"],
        "repeat": commission["repeat"], "n": commission["n"], "turn": commission["turn"],
        "query_truncated": commission["query_truncated"],
        "status": (response.metadata or {}).get("drive_status", response.status.value),
        "correct": not failures, "failures": failures, "ops": ops,
        "specialist_llm_calls": usage["tokens"]["specialist"]["calls"],
        "smart_llm_calls": usage["tokens"]["smart"]["calls"], "other_llm_calls": usage["tokens"]["other"]["calls"],
        "wall_s": wall, "vision_files": len(files), **usage,
        "models": sorted({c["model"] for c in world.meter.since(mark)}),
        "round_trips": 1, "drive_statuses": [(response.metadata or {}).get("drive_status", "")],
        "removed_intent_calls": 0, "commission": commission["query"], "user_message": commission["user_message"],
        "delivered": text[:6000],
    }


async def replay_all(commissions: Sequence[Mapping[str, Any]], gold_by_id: Mapping[str, Mapping[str, Any]],
                     world: World, meter: Meter) -> tuple:
    rows: List[Dict[str, Any]] = []
    for commission in commissions:
        if meter.spent + estimate_next(rows) > meter.max_usd:
            return rows, f"${meter.spent:.4f} spent + ${estimate_next(rows):.4f} would pass --max-usd {meter.max_usd}"
        case = gold_by_id.get(commission["case_id"])
        row = await replay_one(commission, (case or {}).get("gold"), world, case=case)
        rows.append(row)
        print(f"{'+' if row['correct'] else '!'} {row['id']:<34} {row['status']:<14} spec={row['specialist_llm_calls']} "
              f"{row['wall_s']}s ${row['usd']:.4f}" + ("" if row["correct"] else f"  {row['failures']}"))
        if meter.capped:
            return rows, f"the cost cap was reached inside {row['id']}"
    return rows, None


async def run_replay(args: Any) -> int:
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    wanted = {i.strip() for i in args.cases.split(",") if i.strip()}
    if args.replay_commissions is not None:
        report = json.loads(args.replay_commissions.read_text(encoding="utf-8"))
        commissions = commissions_from_report(report, args.replay_design)
        if wanted:
            commissions = [c for c in commissions if c["case_id"] in wanted]
        source = str(args.replay_commissions)
    else:
        commissions = commissions_from_gold(gold, sorted(wanted))
        source = f"{args.gold} (gold commissions)"
    if not commissions:
        raise AbError("there is no work_with_drive_files commission to replay")
    world = await build_world(args, gold)
    try:
        rows, stopped = await replay_all(commissions, {c["id"]: c for c in gold}, world, world.meter)
    finally:
        for undo in reversed(world.restore):
            undo()
    meta = {"stamp": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"), "smart_model": "not called",
            "max_usd": args.max_usd, "spent": round(world.meter.spent, 6), "stopped": stopped,
            "replayed_from": source, "setup": world.setup, "commissions": len(commissions)}
    json_path, md_path = write_report(rows, meta, stem="specialist_replay")
    print(f"report: {json_path}\n        {md_path}")
    return EXIT_STOPPED if stopped else EXIT_OK
