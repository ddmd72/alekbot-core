"""Pure logic of the agent-workspace review (docs/10_rfcs/AGENT_WORKSPACE_RFC.md §16). No I/O.

Inputs: `[Workspace]` log lines (Cloud Logging, 28 days) and Smart's `prompt_content` rows (trace_id, timestamp,
turn, request_text, tool_calls) of the traces that called the workspace or fired a reminder with a workspace file.
Reminder runs are identified per trace (the alert carries `Workspace file:` but no fire key, §8.3); re-runs of a fire
by the same-fire header in a later turn. Output: the §16 measures, per trigger and per origin, as Markdown.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

MUTATING = frozenset({"create", "str_replace", "delete", "rename", "copy", "restore"})
_SIZED_WRITES = frozenset({"create", "str_replace", "copy", "restore"})  # their `bytes` is the new version's size

_LINE = re.compile(
    r"\[Workspace\] op=(?P<op>\S+) user=(?P<user>\S+) path=(?P<path>\S+) origin=(?P<origin>\S+) "
    r"fire=(?P<fire>\S+) had_history=(?P<had_history>\S+) bytes=(?P<bytes>\d+) gen=(?P<gen>\S+) "
    r"result=(?P<result>\S+) ms=(?P<ms>\d+)"
)
_WORKSPACE_LINE = "Workspace file: workspace:"
_SAME_FIRE = "written by an earlier attempt of this same fire"


@dataclass(frozen=True)
class LogLine:
    timestamp: str
    op: str
    user: str
    path: str
    origin: str
    fire: Optional[str]
    had_history: Optional[bool]
    result: str
    nbytes: int


def parse_line(timestamp: str, text: str) -> Optional[LogLine]:
    m = _LINE.search(text or "")
    if m is None:
        return None
    had_history, fire = m["had_history"], m["fire"]
    return LogLine(
        timestamp=timestamp, op=m["op"], user=m["user"], path=m["path"], origin=m["origin"],
        # The log writes <note_id>@<due_at> (§16); the full key is reminder:<note_id>@<due_at>.
        fire=None if fire == "-" else (fire if fire.startswith("reminder:") else f"reminder:{fire}"),
        had_history=None if had_history == "-" else had_history == "1",
        result=m["result"], nbytes=int(m["bytes"]),
    )


@dataclass
class Trace:
    trace_id: str
    first_request: str = ""
    texts: List[str] = field(default_factory=list)
    calls: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)

    @property
    def has_workspace_file(self) -> bool:
        return "[SELF-REMINDER]" in self.first_request and _WORKSPACE_LINE in self.first_request

    @property
    def saw_same_fire_header(self) -> bool:
        return any(_SAME_FIRE in text for text in self.texts[1:])

    def workspace_calls(self) -> List[Dict[str, Any]]:
        return [args for name, args in self.calls if name == "workspace"]


def build_traces(rows: Iterable[Mapping[str, Any]]) -> List[Trace]:
    """Rows ordered by timestamp, turn: a trace's first row is its first turn; traces keep first-seen order."""
    traces: Dict[str, Trace] = {}
    for row in rows:
        trace_id = str(row.get("trace_id") or "")
        trace = traces.setdefault(trace_id, Trace(trace_id=trace_id))
        text = str(row.get("request_text") or "")
        if not trace.texts:
            trace.first_request = text
        trace.texts.append(text)
        raw = row.get("tool_calls")
        if raw:
            for call in json.loads(raw):
                trace.calls.append((str(call.get("name", "")), dict(call.get("args") or {})))
    return list(traces.values())


def classify_trigger(trace: Trace) -> str:
    """reminder: the alert carries a workspace file; skill: a loaded skill body names a workspace file; the rest is a
    user ask or an instruction (§16)."""
    if trace.has_workspace_file:
        return "reminder"
    loaded_skill = any(name == "use_skill" for name, _ in trace.calls)
    if loaded_skill and any('[Skill "' in text and "workspace:" in text for text in trace.texts[1:]):
        return "skill"
    return "user or instruction"


def _worked_from_the_earlier_version(trace: Trace) -> bool:
    """After the same-fire header, a view of the earlier generation comes before the first write (§7.4, §10 row 1)."""
    for args in trace.workspace_calls():
        command = str(args.get("command", ""))
        if command == "view" and str(args.get("generation", "")).strip() not in ("", "0"):
            return True
        if command in MUTATING:
            return False
    return True


@dataclass(frozen=True)
class Growth:
    user: str
    path: str
    sizes: Tuple[int, ...]

    @property
    def writes(self) -> int:
        return len(self.sizes)

    @property
    def first(self) -> int:
        return self.sizes[0]

    @property
    def last(self) -> int:
        return self.sizes[-1]

    @property
    def only_grows(self) -> bool:
        """At least three writes, never smaller than the one before, and larger at the end (§10 row 4)."""
        steps = zip(self.sizes, self.sizes[1:])
        return self.writes >= 3 and self.last > self.first and all(b >= a for a, b in steps)


@dataclass
class Review:
    reminder_runs_with_file: int
    reminder_runs_viewed: int
    recreated_without_restore: List[LogLine]
    same_fire_ignored: List[str]
    growth: List[Growth]
    conflicts: List[LogLine]
    calls_by_trigger: Dict[str, int]
    lines_by_origin: Dict[str, int]

    @property
    def view_rate(self) -> Optional[float]:
        if not self.reminder_runs_with_file:
            return None
        return self.reminder_runs_viewed / self.reminder_runs_with_file


def review(lines: Sequence[LogLine], traces: Sequence[Trace]) -> Review:
    reminder_runs = [t for t in traces if t.has_workspace_file]
    viewed = [t for t in reminder_runs if any(str(a.get("command")) == "view" for a in t.workspace_calls())]
    restored = {(line.user, line.path) for line in lines if line.op == "restore" and line.result == "ok"}
    recreated = [
        line for line in lines
        if line.op == "create" and line.result == "ok" and line.had_history and line.origin != "chat"
        and (line.user, line.path) not in restored
    ]
    sizes: Dict[Tuple[str, str], List[int]] = {}
    for line in sorted(lines, key=lambda line: line.timestamp):
        if line.op in _SIZED_WRITES and line.result == "ok":
            sizes.setdefault((line.user, line.path), []).append(line.nbytes)
    per_trigger: Counter = Counter()
    for trace in traces:
        count = len(trace.workspace_calls())
        if count:
            per_trigger[classify_trigger(trace)] += count
    return Review(
        reminder_runs_with_file=len(reminder_runs),
        reminder_runs_viewed=len(viewed),
        recreated_without_restore=recreated,
        same_fire_ignored=[t.trace_id for t in traces
                           if t.saw_same_fire_header and not _worked_from_the_earlier_version(t)],
        growth=[Growth(user=user, path=path, sizes=tuple(s)) for (user, path), s in sorted(sizes.items())],
        conflicts=[line for line in lines if line.result == "conflict"],
        calls_by_trigger=dict(per_trigger),
        lines_by_origin=dict(Counter(line.origin for line in lines)),
    )


def _rows(items: Sequence[LogLine]) -> List[str]:
    return [
        f"- {i.timestamp} {i.op} {i.path} origin={i.origin} fire={i.fire or '-'} result={i.result}" for i in items
    ] or ["- none"]


def render_report(r: Review, since: str) -> str:
    rate = ("n/a (no reminder run with a workspace file)" if r.view_rate is None
            else f"{r.view_rate:.0%} ({r.reminder_runs_viewed}/{r.reminder_runs_with_file})")
    growing = [g for g in r.growth if g.only_grows]
    lines = [
        f"# Agent workspace review (since {since})",
        "",
        "| Measure | Value | Bar |",
        "|---|---|---|",
        f"| Reminder runs with a workspace_file that view it | {rate} | ≥ 90 % |",
        f"| Files recreated after a deletion without a restore or a user request | "
        f"{len(r.recreated_without_restore)} | 0 |",
        f"| Same-fire re-runs that ignored the earlier version | {len(r.same_fire_ignored)} | reported |",
        f"| Files that only grow | {len(growing)} of {len(r.growth)} written | reported per file |",
        f"| Conflicts | {len(r.conflicts)} | each one read |",
        "| Junk memory facts about workspace operations | manual: search Cabinet → Memory for "
        "'workspace' and 'projects/' | 0 |",
        "",
        "## Workspace calls per trigger",
        *([f"- {k}: {v}" for k, v in sorted(r.calls_by_trigger.items())] or ["- none"]),
        "",
        "## Origin mix (log lines per origin)",
        *([f"- {k}: {v}" for k, v in sorted(r.lines_by_origin.items())] or ["- none"]),
        "",
        "## File growth (bytes of successive writes)",
        *([f"- {g.user} {g.path}: {g.writes} writes, {g.first} → {g.last} B"
           f"{' — only grows' if g.only_grows else ''}" for g in r.growth] or ["- none"]),
        "",
        "## Recreated without restore",
        *_rows(r.recreated_without_restore),
        "",
        "## Same-fire re-runs that ignored the earlier version (trace ids)",
        *([f"- {t}" for t in r.same_fire_ignored] or ["- none"]),
        "",
        "## Conflicts",
        *_rows(r.conflicts),
    ]
    return "\n".join(lines) + "\n"
