"""The drive specialist's one answer shape, built by code from the collector (DRIVE_SPECIALIST_RFC §7).

    Drive result: <status>
    What to do: <line>
    <state line>                      a connection state when no operation ran; a search state when a search ran
    <step>. done: <today's text>      one line per mutating call, in run order
    <step>. not done: <today's text>
    Reading: <line>                   zero or more, chosen by code from the blocks (§7.3)
    Specialist note: <≤ 300 characters>
    [Result r1, step 1: search]
    <verbatim block>
    …

The status is set by code from the outcome classes, never by the model; the model's `question` and `not_done` can only
lower it (§7.1). "What to do" is today's message of the operation that decided the status.

`DriveStatus` (drive_status.py) and `DriveReport` (drive_report.py) are the two other value types of this contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from . import drive_answer_texts as t
from .agent import DeliveryItem
from .drive_collected_result import CollectedResult
from .drive_line import DriveLine
from .drive_op import DriveOp
from .drive_outcome_class import OutcomeClass
from .drive_pass_through import PassThroughCollector
from .drive_report import DriveReport
from .drive_search import DISABLED_TEXT
from .drive_search_line import SearchLine
from .drive_search_texts import REBUILDING_LINE
from .drive_status import DriveStatus


@dataclass(frozen=True)
class DriveAnswer:
    text: str
    status: DriveStatus
    files: List[Dict[str, Any]] = field(default_factory=list)
    drive_context: List[Dict[str, str]] = field(default_factory=list)
    reading: Tuple[str, ...] = ()
    # The note changes' history lines and the owner's receipts, from the collector on every status (design Δ7).
    note_context: List[str] = field(default_factory=list)
    delivery_items: List[DeliveryItem] = field(default_factory=list)


_CLASS_STATUS = {
    OutcomeClass.REFUSED: DriveStatus.REFUSED, OutcomeClass.FAILED: DriveStatus.FAILED,
    OutcomeClass.NOT_FOUND: DriveStatus.NOT_FOUND, OutcomeClass.NOT_CONNECTED: DriveStatus.NOT_CONNECTED,
    OutcomeClass.NOT_POSSIBLE: DriveStatus.NOT_POSSIBLE, OutcomeClass.REBUILDING: DriveStatus.REBUILDING,
    OutcomeClass.INDEXING_OFF: DriveStatus.INDEXING_OFF,
    # a request-shape error the specialist could not fix in its turns is a refusal (§7.2)
    OutcomeClass.REQUEST_ERROR: DriveStatus.REFUSED,
}
# §7.2 row 9: the most informative read failure first
_READ_FAILURE_ORDER = (DriveStatus.NOT_CONNECTED, DriveStatus.REBUILDING, DriveStatus.INDEXING_OFF,
                       DriveStatus.REFUSED, DriveStatus.FAILED, DriveStatus.NOT_FOUND)
_DONE = frozenset({OutcomeClass.OK})
# A correctable tool error: a later call of the same operation in a later step settles it (the model fixed it).
_CORRECTABLE = frozenset({OutcomeClass.REQUEST_ERROR, OutcomeClass.NOT_POSSIBLE})


def compose_answer(collector: PassThroughCollector, report: DriveReport, *, cut: bool = False,
                   drive_line: DriveLine = DriveLine.CONNECTED,
                   search_line: SearchLine = SearchLine.ON) -> DriveAnswer:
    """`cut`: the soft deadline, an engine exception or the turn limit ended the run before its report (§7.2: the
    status is then `unfinished` whatever finished). A correctable error the model fixed is left out of the status and
    of the read blocks, but a change's op line is never dropped (§7.2). `search_line` is the preflight's: when it is not
    `on`, no search ran and the answer holds reads only, today's search state text is the state line (X6)."""
    settled = _settled(collector.results)
    results = [r for r in collector.results if r.handle not in settled]
    all_changes = [r for r in collector.results if r.outcome.is_change]
    changes = [r for r in results if r.outcome.is_change]
    reads = [r for r in results if not r.outcome.is_change]
    done = [r for r in changes if r.outcome.cls in _DONE]
    failed = [r for r in changes if r.outcome.cls not in _DONE and r.outcome.cls is not OutcomeClass.STILL_RUNNING]
    content = [r for r in reads if r.outcome.success and r.outcome.cls is OutcomeClass.OK]
    running = next((r for r in results if r.outcome.cls is OutcomeClass.STILL_RUNNING), None)

    state_line = ""
    by_change = False   # the status comes from a change: the lookup search before it gets no reading lines (X7)
    if running is not None:
        status, line = DriveStatus.STILL_RUNNING, running.outcome.text
    elif cut or any(r.outcome.cls is OutcomeClass.NOT_FINISHED for r in results):
        status, line = DriveStatus.UNFINISHED, t.UNFINISHED_LINE
    elif done and (failed or report.question or report.not_done):
        by_change = True
        status = DriveStatus.PARTIAL
        line = " ".join(part for part in (
            t.ONLY_PART, report.question,
            t.PARTIAL_NOT_DONE.format(not_done=report.not_done) if report.not_done else "") if part)
    elif failed:
        by_change = True
        first = failed[0].outcome
        status = _CLASS_STATUS.get(first.cls, DriveStatus.FAILED)
        line = t.NOT_ATTACHED if first.cls is OutcomeClass.NOT_POSSIBLE else first.text
    elif report.question:
        status, line = DriveStatus.AMBIGUOUS, report.question
    elif not results and drive_line in (DriveLine.NOT_CONNECTED, DriveLine.RECONNECT_NEEDED):
        status, line = DriveStatus.NOT_CONNECTED, _NOT_CONNECTED_TEXT
        state_line = line
    elif not results and drive_line is DriveLine.NOT_CONFIGURED:
        status, line = DriveStatus.FAILED, _NOT_CONFIGURED_TEXT
        state_line = line
    elif report.not_done:
        status, line = DriveStatus.NOT_POSSIBLE, t.NOTHING_DONE.format(why=report.not_done)
    elif not results:
        status, line = DriveStatus.NOT_POSSIBLE, t.NOTHING_DONE.format(why=report.note or t.NOTHING_RAN)
    elif done:
        by_change = True
        status, line = DriveStatus.OK, t.DONE_LINE
    elif content:
        status, line = DriveStatus.OK, t.ANSWER_FROM_RESULTS
    else:
        status, line = _worst_read(reads)

    if not state_line:
        state_line = _search_state_line(results)
    if not state_line and reads and not changes and not any(r.outcome.op is DriveOp.SEARCH for r in results):
        state_line = _preflight_search_text(search_line)
    blocks = _read_blocks(reads)
    reading = reading_lines([b for b in blocks if b.outcome.op is not DriveOp.SEARCH] if by_change else blocks)

    out = [t.STATUS_HEAD.format(status=status.value), t.WHAT_TO_DO.format(line=line)]
    if state_line and state_line != line:
        out.append(state_line)
    for r in all_changes:
        verdict = (t.OP_DONE if r.outcome.cls in _DONE
                   else t.OP_RUNNING if r.outcome.cls is OutcomeClass.STILL_RUNNING else t.OP_NOT_DONE)
        out.append(t.OP_LINE.format(step=r.step, verdict=verdict, text=r.outcome.text))
    out.extend(t.READING_PREFIX + line for line in reading)
    if report.note:
        out.append(t.NOTE_PREFIX + report.note[: t.NOTE_MAX_CHARS])
    change_steps = sorted({r.step for r in changes})
    for block in blocks:
        later = [s for s in change_steps if s > block.step]
        if later and block.kind == t.KIND_LISTING:
            out.append(t.RESULT_HEAD_BEFORE.format(handle=block.handle, step=block.step, kind=block.kind,
                                                   steps=_steps(later)))
        else:
            out.append(t.RESULT_HEAD.format(handle=block.handle, step=block.step, kind=block.kind))
        out.append(block.outcome.text)
    return DriveAnswer(text="\n".join(out), status=status, files=collector.files(),
                       drive_context=collector.drive_context(), reading=tuple(reading),
                       note_context=collector.note_context(), delivery_items=collector.delivery_items())


def reading_lines(blocks: Sequence[CollectedResult]) -> List[str]:
    """§7.3: each trigger adds exactly its lines, once, in a fixed order."""
    lines: List[str] = []

    def add(*items: str) -> None:
        lines.extend(item for item in items if item not in lines)

    searches = [b.outcome for b in blocks if b.outcome.op is DriveOp.SEARCH]
    cards = sum(s.facts.cards for s in searches)
    if cards:
        add(t.R_FIND, t.R_TIERS, t.R_TRUST, t.R_RELATED, t.R_COVERAGE, t.R_MANY, t.R_ALL)
    if any(s.facts.passage for s in searches):
        add(t.R_PASSAGE)
    if any(s.facts.accuracy for s in searches):
        add(t.R_ACCURACY, t.R_ACCURACY_FIND, t.R_ACCURACY_VALUE)
    if any(b.outcome.facts.owner_note for b in blocks):
        add(t.R_OWNER_NOTE)
    if any(s.facts.scan for s in searches):
        add(t.R_SCAN)
    if any(b.outcome.facts.inaccessible for b in blocks):
        add(t.R_NO_ACCESS)
    if any(s.facts.image for s in searches):
        add(t.R_PHOTOS)
    if cards >= 2:
        add(t.R_VERSIONS)
    # the deleted-files search reads the ledger, not the index: its not-found gets no index lines (X7)
    if any(b.kind == t.KIND_SEARCH and (b.outcome.cls is OutcomeClass.NOT_FOUND or b.outcome.facts.coverage_incomplete
                                         or b.outcome.facts.building) for b in blocks if b.outcome.op is DriveOp.SEARCH):
        add(t.R_BUILDING, t.R_NOT_SEARCHED, t.R_FOLDER_CARD)
    # Only a file's content opens the "numbers / document vs memory / not read" lines: a body or a vision file, not a
    # refusal or the line that a video was sent to the chat (M2).
    if any(b.outcome.op is DriveOp.OPEN and b.kind == t.KIND_OPEN and b.outcome.cls is OutcomeClass.OK
           and (b.outcome.body or b.outcome.file_data) for b in blocks):
        add(t.R_NUMBERS, t.R_DOC_VS_MEMORY, t.R_NOT_READ)
    return lines


# Today's texts for a drive state no operation reported (§7.2 row 6); equal to DriveOperations' constants, which a
# test pins (the domain cannot import infrastructure).
_NOT_CONNECTED_TEXT = ("The user's drive is not connected or its access expired. "
                       "Ask them to connect it in the Cabinet.")
_NOT_CONFIGURED_TEXT = "The drive is not configured."


def _settled(results: Sequence[CollectedResult]) -> set[str]:
    """The handles of correctable errors the model fixed: a later call of the same operation, in a later step, that
    was not itself a correctable error, settles one earlier error (the earliest first). A settled error is out of the
    status and, for a read, out of the blocks; for a change its op line stays (§7.2)."""
    pending: Dict[DriveOp, List[CollectedResult]] = {}
    dropped: set[str] = set()
    for r in results:
        op = r.outcome.op
        if r.outcome.cls in _CORRECTABLE:
            pending.setdefault(op, []).append(r)
            continue
        queue = pending.get(op, [])
        earlier = next((e for e in queue if e.step < r.step), None)
        if earlier is not None:
            queue.remove(earlier)
            dropped.add(earlier.handle)
    return dropped


def _read_blocks(reads: Sequence[CollectedResult]) -> List[CollectedResult]:
    """Every read in run order, dropping only byte-identical repeats of the same read (§6)."""
    seen: set[Tuple[str, str]] = set()
    blocks: List[CollectedResult] = []
    for r in reads:
        key = (r.kind, r.outcome.text)
        if key in seen:
            continue
        seen.add(key)
        blocks.append(r)
    return blocks


def _worst_read(reads: Sequence[CollectedResult]) -> Tuple[DriveStatus, str]:
    # A search that found nothing: its block carries the text; "What to do" is the pointer, not the whole text (X4).
    statuses = [(_CLASS_STATUS.get(r.outcome.cls, DriveStatus.FAILED),
                 t.ANSWER_FROM_RESULTS if r.outcome.op is DriveOp.SEARCH and r.outcome.cls is OutcomeClass.NOT_FOUND
                 else r.outcome.text) for r in reads]
    for wanted in _READ_FAILURE_ORDER:
        for status, text in statuses:
            if status is wanted:
                return status, text
    return DriveStatus.UNFINISHED, t.UNFINISHED_LINE


def _search_state_line(results: Sequence[CollectedResult]) -> str:
    """A search's own state text, raised only on an answer that ran a search (§7.2): `indexing_off` or `rebuilding`."""
    for r in results:
        o = r.outcome
        if o.op is DriveOp.SEARCH and o.cls is OutcomeClass.INDEXING_OFF:
            return o.text
        if o.op is DriveOp.SEARCH and o.cls is OutcomeClass.REBUILDING:
            return REBUILDING_LINE if REBUILDING_LINE in o.text else o.text
    return ""


def _preflight_search_text(line: SearchLine) -> str:
    """Today's search state text for a preflight search line that is not `on`, raised when no search ran (X6)."""
    if line is SearchLine.OFF:
        return DISABLED_TEXT
    if line is SearchLine.REBUILDING:
        return REBUILDING_LINE
    return ""


def _steps(steps: Sequence[int]) -> str:
    """`step 3`, `steps 3–5`, `steps 2, 4`."""
    if len(steps) == 1:
        return f"step {steps[0]}"
    if steps == list(range(steps[0], steps[-1] + 1)):
        return f"steps {steps[0]}–{steps[-1]}"
    return "steps " + ", ".join(str(s) for s in steps)
