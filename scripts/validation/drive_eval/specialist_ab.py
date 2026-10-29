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

Answer from content (USER_DRIVE_INDEXING_RFC §28.5, plan S2.6): `--gold gold_specialist_afc.json` runs the branch's
prompt files (`--prompts-from-snapshot`) on the invented Acme `R&D` documents, each case under its `origin` (chat,
background, call or fallback: the context the specialist reads), and reports per case the opens, the passage chunks that
reached Smart, reference-point recall, writes that reached the stub, gate refusals counted from the run's own recorded
outcomes (the `WRITE_AFTER_PASSAGES` line), stop reasons and follow-ups, and per family and origin in the markdown.
`--owner-notes` applies the invented owner notes after the cached index is loaded (never part of the cache key).
`--replay-gold-commissions` replays the gold's own `commission` texts (the method-dictating ones) on the specialist.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.agents.base_agent import BaseAgent
from src.domain.drive_answer_texts import WRITE_AFTER_PASSAGES
from src.domain.drive_note_tool_texts import NOTE_WRITE_TOOLS
from src.domain.history_context_block import history_context_block
from src.domain.drive_tool_texts import (
    APPEND_TO_FILE, COPY_FILE_TO_DRIVE, CREATE_FOLDER, DELETE_DRIVE_ITEM, FIND_PASSAGES, MOVE_FILE, READ_PASSAGES,
    REPLACE_FILE_CONTENT, SAVE_TEXT_FILE)
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

# the families of gold_specialist_afc.json (plan §5 S2); a case's `kind` is its family
AFC_FAMILIES = ("knowledge", "value_in_content", "everything_about", "follow_up", "whole_file", "method_dictating",
                "injection_on_topic", "low_accuracy")
CONTENT_FAMILIES = frozenset({"knowledge", "value_in_content", "everything_about", "follow_up", "whole_file",
                              "method_dictating", "injection_on_topic"})   # the families the 0.90 recall bar covers
RECALL_BAR = 0.90   # plan §5 S2: reference-point recall in what reaches Smart over the content families
ORIGINS = ("chat", "background", "call", "fallback")
# the context a run carries, as the specialist reads it (origin_label in drive_agent.py; the A14 gate)
ORIGIN_CONTEXT: Dict[str, Dict[str, Any]] = {
    "chat": {"interactive_delivery": True}, "background": {"run_origin": "background:reminder"},
    "call": {"run_origin": "voice:ask_alek"}, "fallback": {}}
PASSAGE_OPS = frozenset({FIND_PASSAGES, READ_PASSAGES})
WRITE_TOOLS = frozenset({COPY_FILE_TO_DRIVE, SAVE_TEXT_FILE, CREATE_FOLDER, MOVE_FILE, APPEND_TO_FILE,
                         REPLACE_FILE_CONTENT, DELETE_DRIVE_ITEM, *NOTE_WRITE_TOOLS})
_PASSAGE_HEAD = re.compile(r"passages (\d+)[\u2013-](\d+) of \d+\]")
KINDS = frozenset({"content", "value_file_word", "value_no_file_word", "find", "find_then_open", "translit",
                   "lead_in_results", "lead_in_opened_text", "versions", "batch_move", "vague_change", "save_text",
                   "deleted_search", "transcript", "recording_link", "photo", "negative_memory", "negative_web",
                   "negative_reminder", *AFC_FAMILIES})
GOLD_KEYS = frozenset({"drive", "answer_must_include", "answer_must_not_include", "must_open", "must_not_open",
                       "opened_any", "ops_must_include", "ops_forbidden", "intents_must_include", "no_writes"})
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
        problems += afc_case_problems(case)
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
                or g.get("intents_must_include") or g.get("opened_any") or not g["drive"] or g.get("no_writes")
                or g.get("must_open") or g.get("must_not_open") or case.get("reference_points")):
            problems.append(f"{cid}: gold checks nothing")
    if problems:
        raise AbError("malformed specialist eval gold: " + "; ".join(problems))


def _point_ok(point: Any) -> bool:
    if isinstance(point, str):
        return bool(point.strip())
    return isinstance(point, list) and bool(point) and all(isinstance(w, str) and w.strip() for w in point)


def afc_case_problems(case: Mapping[str, Any]) -> List[str]:
    """The checks on the fields of the answer-from-content gold (all optional on an older case): `family` equals `kind`,
    `origin` is one of ORIGINS, `reference_points` are texts (or lists of alternatives), `commission` a text."""
    cid, problems = case.get("id"), []
    if "family" in case and case["family"] != case.get("kind"):
        problems.append(f"{cid}: family {case['family']!r} must equal kind {case.get('kind')!r}")
    if "origin" in case and case["origin"] not in ORIGINS:
        problems.append(f"{cid}: origin must be one of {list(ORIGINS)}")
    points = case.get("reference_points")
    if points is not None and not (isinstance(points, list) and points and all(_point_ok(x) for x in points)):
        problems.append(f"{cid}: reference_points must be a non-empty list of texts or lists of alternatives")
    if "commission" in case and not (isinstance(case["commission"], str) and case["commission"].strip()):
        problems.append(f"{cid}: commission must be a text")
    cites = case.get("cites")
    if cites is not None and not (isinstance(cites, list) and cites and all(isinstance(x, str) and x for x in cites)):
        problems.append(f"{cid}: cites must be a non-empty list of paths")
    return problems


def needs_owner_notes(gold: Sequence[Mapping[str, Any]]) -> bool:
    return any(c.get("needs_owner_notes") for c in gold)


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
    present = [w for w in g.get("answer_must_not_include") or [] if w.casefold() in spaced.casefold()]
    if present:
        failures.append(f"answer holds {present}")
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
    """The session pair ConversationHandler writes: the answer, with every `*_context` block in full_text, rendered by
    the handler's own `history_context_block` (the drive's refs as one line per file)."""
    full = answer
    for key, value in (metadata or {}).items():
        block = history_context_block(key, value)
        if block:
            full += "\n\n" + block
    now = time.time()
    return [Message(role="user", parts=[MessagePart(text=text)], created_at=now),
            Message(role="model", parts=[MessagePart(text=answer, full_text=full)], created_at=now + 1)]


def answer_text(response: Any) -> str:
    result = getattr(response, "result", None)
    if result is None:
        return f"[FAILED] {getattr(response, 'error', '')}"
    return str(getattr(result, "text", result))


# -- answer from content: per-case metrics ------------------------------------------------------------------------------------

def _squash(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").replace("\u202f", " ").split()).casefold()


def point_found(point: Any, haystack: str) -> bool:
    """A reference point (a text, or a list of alternatives) in `haystack`, case and spacing aside."""
    alternatives = [point] if isinstance(point, str) else list(point)
    return any(_squash(a) in haystack for a in alternatives)


def passage_chunks(text: str) -> int:
    """The distinct passage chunks a text delivers: each head line `[… passages i–j of N]` counts j - i + 1 once."""
    return sum(int(j) - int(i) + 1 for i, j in {m.groups() for m in _PASSAGE_HEAD.finditer(text)})


def afc_metrics(ops: Sequence[Mapping[str, Any]], delivered: str, trace: Mapping[str, Sequence[str]],
                points: Sequence[Any] = (), answer: str = "", points_in_answer: bool = False,
                follow_ups: int = 0) -> Dict[str, Any]:
    """What one run did, from the op log, the text that reached Smart (`delivered`: the specialist's answers) and the
    specialist's own LLM turns (`trace`: finish reasons and tool names). Gate refusals are COUNTED, not inferred: the
    specialist's refused write is recorded in its outcomes as the `WRITE_AFTER_PASSAGES` line, which code puts in the
    answer Smart receives. `unrecorded_writes` = attempted write tool calls neither executed (stub) nor refused."""
    haystack = _squash(delivered + ("\n" + answer if points_in_answer else ""))
    found = [p for p in points if point_found(p, haystack)]
    attempted = sum(1 for t in trace.get("tools", ()) if t in WRITE_TOOLS)
    reached = sum(1 for o in ops if o.get("stubbed"))
    refused = delivered.count(WRITE_AFTER_PASSAGES)
    finish = Counter(trace.get("finish", ()))
    return {
        "opens": len(drive_opens(ops)), "passage_calls": sum(1 for o in ops if o["op"] in PASSAGE_OPS),
        "passage_chunks": passage_chunks(delivered), "points_total": len(points), "points_found": len(found),
        "recall": round(len(found) / len(points), 4) if points else None,
        "missing_points": [p for p in points if p not in found],
        "attempted_writes": attempted, "writes_reached_stub": reached, "gate_refusals": refused,
        "unrecorded_writes": max(0, attempted - reached - refused),
        "stop_reasons": dict(finish), "max_tokens_stops": finish.get("max_tokens", 0), "follow_ups": follow_ups}


def afc_failures(case: Mapping[str, Any], metrics: Mapping[str, Any], delivered: str, *, answer_known: bool = True
                 ) -> List[str]:
    """The bars of plan §5 S2 that one run can fail by itself (the 0.90 aggregate is `afc_summary`'s)."""
    failures: List[str] = []
    in_answer = bool(case.get("reference_in_answer"))
    if metrics["points_total"] and metrics["points_found"] == 0 and (answer_known or not in_answer):
        failures.append("no reference point reached Smart")
    cites = case.get("cites")
    if cites and not any(c.casefold() in delivered.casefold() for c in cites):
        failures.append(f"what reached Smart cites none of {cites}")
    if (case.get("gold") or {}).get("no_writes"):
        if metrics["writes_reached_stub"]:
            failures.append(f"{metrics['writes_reached_stub']} write(s) reached the stub")
        if metrics["unrecorded_writes"]:
            failures.append(f"{metrics['unrecorded_writes']} attempted write(s) neither refused by the gate nor stubbed")
    return failures


def origin_context(origin: str) -> Dict[str, Any]:
    """The context keys of a run's origin (ORIGIN_CONTEXT): a copy."""
    if origin not in ORIGIN_CONTEXT:
        raise AbError(f"unknown origin {origin!r}")
    return dict(ORIGIN_CONTEXT[origin])


class SpecialistTrace:
    """The drive specialist's own LLM turns: each response's finish reason and the tools it called. Installed over
    BaseAgent._call_llm after the meter (the meter sees every call too)."""

    def __init__(self) -> None:
        self.finish: List[str] = []
        self.tools: List[str] = []

    def mark(self) -> Tuple[int, int]:
        return len(self.finish), len(self.tools)

    def since(self, mark: Tuple[int, int]) -> Dict[str, List[str]]:
        return {"finish": self.finish[mark[0]:], "tools": self.tools[mark[1]:]}

    def install(self) -> Callable[[], None]:
        original = BaseAgent._call_llm
        trace = self

        async def traced(agent: BaseAgent, request: Any, turn: int = 0, **kwargs: Any) -> Any:
            response = await original(agent, request, turn, **kwargs)
            if agent.config.agent_type == SPECIALIST_TYPE:
                reason = getattr(response, "finish_reason", None)
                trace.finish.append(str(getattr(reason, "value", reason) or "unreported"))
                trace.tools.extend(str(c.name) for c in (getattr(response, "tool_calls", None) or []))
            return response

        BaseAgent._call_llm = traced   # type: ignore[method-assign]

        def uninstall() -> None:
            BaseAgent._call_llm = original   # type: ignore[method-assign]
        return uninstall


@dataclass
class RecordingDispatch(DryRunDispatch):
    """DryRunDispatch that also keeps the text each drive commission returned to Smart (`delivered`)."""

    async def __call__(self, engine: Any, tool_call: Any, context: Dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        result = await super().__call__(engine, tool_call, context, *args, **kwargs)
        if self.calls and self.calls[-1].get("intent") == Intent.WORK_WITH_DRIVE_FILES:
            self.calls[-1]["delivered"] = str(getattr(result, "result_str", "") or "")   # appended just before returning
        return result


def delivered_text(calls: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(str(c.get("delivered", "")) for c in calls if c.get("intent") == Intent.WORK_WITH_DRIVE_FILES)


async def run_case(case: Mapping[str, Any], *, smart: Any, dispatcher: DryRunDispatch, drive_log: Any, meter: Meter,
                   sessions: InMemorySessions, user_id: str, account_id: str, repeat: int = 0,
                   request_context: Optional[Callable[[], Any]] = None, trace: Optional[SpecialistTrace] = None
                   ) -> Dict[str, Any]:
    """One case (all its turns): the row of the report. `origin` (default chat) is the context the run carries; a
    case with a `family` also reports the answer-from-content metrics (`afc`)."""
    from src.domain.agent import AgentIntent, AgentMessage
    from src.domain.turn_clock import CURRENT_TURN_CLOCK

    drive_log.clear()
    dispatcher.calls.clear()
    dispatcher.isolate = bool(case.get("isolate"))
    session_id = f"{user_id}:drive_specialist_eval:{case['id']}:{repeat}"
    mark = meter.mark()
    trace_mark = trace.mark() if trace is not None else (0, 0)
    origin = str(case.get("origin") or "chat")
    started = time.monotonic()
    answer, error = "", ""
    turn_rows: List[Dict[str, Any]] = []
    for text in case["turns"]:
        turn_mark, turn_calls = meter.mark(), len(dispatcher.calls)
        msg = AgentMessage.create(
            sender="drive_specialist_eval", recipient=smart.agent_id, intent=AgentIntent.QUERY, payload={"text": text},
            context={"user_id": user_id, "account_id": account_id, "session_id": session_id,
                     "current_message_parts": [MessagePart(text=text)], **origin_context(origin)})
        token = CURRENT_TURN_CLOCK.set(interactive_turn_clock()) if origin == "chat" else None
        turn_started = time.monotonic()
        try:
            if request_context is not None:
                async with request_context():
                    response = await smart.execute(msg)
            else:
                response = await smart.execute(msg)
        finally:
            if token is not None:
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
    afc: Dict[str, Any] = {}
    if case.get("family"):
        delivered = delivered_text(calls)
        afc = afc_metrics(ops, delivered, trace.since(trace_mark) if trace is not None else {},
                          case.get("reference_points") or (), answer, bool(case.get("reference_in_answer")),
                          follow_ups=max(0, len(commissions) - 1))
        if not error:
            failures = failures + afc_failures(case, afc, delivered)
    return {
        **({"family": case["family"], "origin": origin, "afc": afc} if afc else {}),
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


def afc_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Per family and per origin, and the 0.90 recall over the content families (plan §5 S2); {} without afc rows."""
    afc_rows = [r for r in rows if r.get("afc")]
    if not afc_rows:
        return {}
    families: Dict[str, Dict[str, Any]] = {}
    origins: Dict[str, Dict[str, Any]] = {}
    for r in afc_rows:
        m = r["afc"]
        f = families.setdefault(r["family"], {"cases": 0, "correct": 0, "opens": 0, "points_found": 0,
                                              "points_total": 0, "passage_chunks": 0})
        f["cases"] += 1
        f["correct"] += bool(r.get("correct"))
        for k in ("opens", "points_found", "points_total", "passage_chunks"):
            f[k] += int(m[k])
        o = origins.setdefault(r["origin"], {"cases": 0, "stop_reasons": Counter(), "max_tokens_stops": 0,
                                             "follow_ups": 0, "gate_refusals": 0, "writes_reached_stub": 0,
                                             "attempted_writes": 0})
        o["cases"] += 1
        o["stop_reasons"].update(m["stop_reasons"])
        for k in ("max_tokens_stops", "follow_ups", "gate_refusals", "writes_reached_stub", "attempted_writes"):
            o[k] += int(m[k])
    for o in origins.values():
        o["stop_reasons"] = dict(o["stop_reasons"])
    found = sum(r["afc"]["points_found"] for r in afc_rows if r["family"] in CONTENT_FAMILIES)
    total = sum(r["afc"]["points_total"] for r in afc_rows if r["family"] in CONTENT_FAMILIES)
    recall = round(found / total, 4) if total else None
    return {"families": families, "origins": origins, "content_recall": recall,
            "recall_ok": recall is not None and recall + 1e-9 >= RECALL_BAR,
            "max_tokens_review": any(o["max_tokens_stops"] for o in origins.values())}


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
    afc = afc_summary(rows)
    if afc:
        lines += ["", "| family | cases | correct | opens | passage chunks | reference points |", "|---|---|---|---|---|---|"]
        for name, f in sorted(afc["families"].items()):
            lines.append(f"| {name} | {f['cases']} | {f['correct']} | {f['opens']} | {f['passage_chunks']} "
                         f"| {f['points_found']}/{f['points_total']} |")
        lines += ["", "| origin | cases | stop reasons | max_tokens stops | follow-ups | attempted writes | gate refusals "
                      "| writes at the stub |", "|---|---|---|---|---|---|---|---|"]
        for name, o in sorted(afc["origins"].items()):
            lines.append(f"| {name} | {o['cases']} | {o['stop_reasons'] or '-'} | {o['max_tokens_stops']} "
                         f"| {o['follow_ups']} | {o['attempted_writes']} | {o['gate_refusals']} "
                         f"| {o['writes_reached_stub']} |")
        recall = afc["content_recall"]
        lines.append(f"Reference-point recall over the content families: "
                     f"{'n/a' if recall is None else f'{recall:.3f}'} (bar {RECALL_BAR}): "
                     + ("met" if afc["recall_ok"] else "**missed**"))
        if afc["max_tokens_review"]:
            lines.append("**A max_tokens stop occurred: A15's budget review is triggered.**")
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
    parser.add_argument("--prompts-from-snapshot", action="store_true",
                        help="serve the drive specialist's prompt files and Smart's PROTOCOL_AGENT_SELECTION from "
                             "prompts_snapshot/ (the \"after\", before any upload); default: the Firestore prompts")
    parser.add_argument("--specialist-provider", choices=("claude", "openai"), default=None,
                        help="the drive specialist's provider, in memory only (default: the user's config)")
    parser.add_argument("--specialist-tier", choices=("eco", "balanced", "performance"), default=None,
                        help="the drive specialist's tier, in memory only (default: the user's config)")
    parser.add_argument("--owner-notes", type=Path, default=None,
                        help="owner notes to apply in memory after the cached index loads (owner_notes_afc.json); "
                             "required by a gold with needs_owner_notes cases")
    parser.add_argument("--replay-gold-commissions", action="store_true",
                        help="replay the gold's own `commission` texts on the specialist only (no Smart)")
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
    trace: Optional[SpecialistTrace] = None
    setup: Optional[Mapping[str, str]] = None


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
    from .dry_run_drive_notes import DryRunDriveNotes
    from .enrichment_snapshot import EnrichmentSnapshot
    from .g3_overrides import (
        PROMPTS_FIRESTORE, PROMPTS_SNAPSHOT, install_snapshot_prompts, run_setup, setup_line, specialist_config)
    from .index_cache import load_or_build
    from .owner_notes_overlay import apply_owner_notes, load_owner_notes
    from .parse_cache import ParseCache
    from .runner import EvalClock, _EnabledUsers
    from .search_v2_eval import index_v2
    from .smart_dry_run import DryRunSkills, RecordingMemoryFacts, annotate_drive_read, g3_drive
    from .specialist_fixture import build_drive_fixture

    owner_notes_path = getattr(args, "owner_notes", None)
    if needs_owner_notes(gold) and owner_notes_path is None:
        raise AbError("the gold has cases that need the owner notes: pass --owner-notes "
                      "tests/fixtures/drive_eval/owner_notes_afc.json")
    restore: List[Callable[[], None]] = [disable_billing_writes()]
    drive_log = DriveOpLog()
    dry_run = DryRunDriveOperations(drive_log, annotate=annotate_drive_read)
    dry_run.install()
    restore.append(dry_run.uninstall)
    note_dry_run = DryRunDriveNotes(drive_log)   # the notes' writes go through DriveNoteService, not DriveOperations
    note_dry_run.install()
    restore.append(note_dry_run.uninstall)
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
    if owner_notes_path is not None:   # after the cache: a note change never costs a rebuild (plan CTX.5)
        print(f"owner notes: {apply_owner_notes(repo, load_owner_notes(owner_notes_path))} record(s) noted")
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
    from_snapshot = bool(getattr(args, "prompts_from_snapshot", False))
    provider, tier = getattr(args, "specialist_provider", None), getattr(args, "specialist_tier", None)
    setup = run_setup(PROMPTS_SNAPSHOT if from_snapshot else PROMPTS_FIRESTORE, provider, tier)
    if from_snapshot:
        install_snapshot_prompts(container.assembly_service)   # before any agent or prompt is built
    print(setup_line(setup))
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
    config = specialist_config(user_context.user_profile.config, provider, tier)
    if getattr(args, "replay_timezone", ""):
        config = config.model_copy(update={"timezone": args.replay_timezone})
    fixture = build_drive_fixture(user_id, drive, search, user_config=config,
                                  prompt_builder=user_context.prompt_builder, context_builder=container.context_builder)
    dispatcher = RecordingDispatch(DelegationEngine.dispatch, fixture.engine, frozenset(_READ_ONLY_INTENTS))
    original_dispatch = DelegationEngine.dispatch

    async def guarded(self: Any, tool_call: Any, context: Dict[str, Any], *a: Any, **kw: Any) -> ToolResult:
        return await dispatcher(self, tool_call, context, *a, **kw)

    DelegationEngine.dispatch = guarded   # type: ignore[method-assign]

    def undo_dispatch() -> None:
        DelegationEngine.dispatch = original_dispatch   # type: ignore[method-assign]
    restore.append(undo_dispatch)
    meter = Meter(max_usd=args.max_usd)
    restore.append(meter.install())
    trace = SpecialistTrace()
    restore.append(trace.install())   # after the meter: undone first, so the meter restores the original
    return World(smart, fixture, dispatcher, drive_log, sessions, meter, user_id, account_id, restore, trace, setup)


async def _run(args: argparse.Namespace) -> int:
    from src.domain.request_context import RequestContext

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    cases = select_cases(gold, args.cases)
    world = await build_world(args, gold)
    try:
        async def one(case: Mapping[str, Any], *, repeat: int) -> Dict[str, Any]:
            return await run_case(case, smart=world.smart, dispatcher=world.dispatcher, drive_log=world.drive_log,
                                  meter=world.meter, sessions=world.sessions, user_id=world.user_id,
                                  account_id=world.account_id, repeat=repeat, trace=getattr(world, "trace", None),
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
            "repeats": args.repeats, "setup": getattr(world, "setup", None)}
    json_path, md_path = write_report(rows, meta, comparison)
    print(markdown(rows, meta, comparison))
    print(f"report: {json_path}\n        {md_path}")
    return EXIT_STOPPED if stopped else EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
    try:
        args = _args(argv)
        if args.replay_commissions is not None or args.replay_gold_commissions:
            from .specialist_replay import run_replay
            return asyncio.run(run_replay(args))
        return asyncio.run(_run(args))
    except AbError as exc:
        print(f"drive specialist eval: no run — {exc}", file=sys.stderr)
        return EXIT_SETUP


if __name__ == "__main__":
    sys.exit(main())
