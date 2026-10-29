"""The drive specialist's tools, served locally by its DelegationEngine (DRIVE_SPECIALIST_RFC §5.4, §6, §8.1).

Built per execution, the `workspace_tools.py` pattern. Every handler calls exactly one DriveOperations method and
records its outcome in the execution's pass-through collector; the model's tool result is what the specialist needs
to choose its next step: search results and listings in full, a stand-in for an opened body, one line per change.

- Mutations first await a per-execution ordering lock: the engine creates a batch's tasks in emission order and
  asyncio.Lock wakes waiters FIFO, so a batch applies in the order the model wrote it (the per-user mutation lock,
  shield and still-running stay in DriveOperations).
- The soft deadline: a call after it is not started; a read runs under `wait_for(…, time left)`; a mutation waits
  `max(1 s, min(100 s, time left − 10 s))`, then today's still-running path. A handler cancelled mid-call (the agent
  cancels its engine at the deadline or on still-running) records the call before re-raising: a read as not finished,
  a change queued behind the batch as not started, a change already started as still running (it goes on under
  DriveOperations' shield, so it is never reported "not done"). After a still-running outcome in this run, a change
  that wakes on the ordering lock is not started (X1, cross-cut review).
- A `search_drive` identical to one already run in this commission is not run again (§5.4).
- `save_text_file` / `append_to_file` name a `file_contents` item; code writes that item's text (§4.2). An unknown
  item is refused before any service call.
- `find_passages` / `read_passages` (USER_DRIVE_INDEXING_RFC §28.3 A3): reads, shown to the model in full. A find
  passes the run's ledger (chunks shown, folders shown, files described), so it returns new passages only and prints
  no context line twice; a range read passes only the two context sets (a named range is delivered whole).
- The write gate (§28.3 A14, OQ-A1): on a run that is not a chat turn, a change (a note write included) in a step
  after the one whose passages text reached the model is refused; a change co-emitted with that read runs.
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence

from ..domain import drive_answer_texts as t
from ..domain import drive_tool_texts as tools
from ..domain.drive_collected_result import CollectedResult
from ..domain.drive_content_gate import content_gate_applies, write_refused
from ..domain.drive_open_bound_text import BoundText
from ..domain.drive_open_cut import stand_in_cut
from ..domain.drive_open_texts import ROUTE_SPECIALIST
from ..domain.drive_file_content_item import FileContentItem
from ..domain.drive_op import DriveOp
from ..domain.drive_outcome import DriveOutcome
from ..domain.drive_outcome_class import OutcomeClass
from ..domain.drive_pass_through import PassThroughCollector, search_key
from ..domain.llm import ToolCall
from ..domain.skill import DELIVERED_REF_PREFIXES, SKILL_REF_PREFIX
from ..domain.user_drive import MOVE_NEEDS_TARGET, drive_label, is_drive_ref
from ..domain.workspace import WORKSPACE_REF_PREFIX
from ..utils.logger import logger
from .delegation_engine import LocalToolHandler, ToolResult
from .drive_operations import STILL_RUNNING

if TYPE_CHECKING:
    from .drive_operations import DriveOperations

MUTATION_WAIT_MAX_S = 100.0   # today's wait inside FileManagement's 120 s timeout (USER_DRIVE_RFC §4.13)
MUTATION_WAIT_MIN_S = 1.0
MUTATION_WAIT_MARGIN_S = 10.0

_Call = Callable[[], Awaitable[DriveOutcome]]


class DriveToolRun:
    """One commission's tool handlers. `step` is the model turn now running; the agent sets it before each call."""

    def __init__(self, ops: "DriveOperations", collector: PassThroughCollector, items: Sequence[FileContentItem],
                 context: Mapping[str, Any], user_id: str, *, deadline: float,
                 on_still_running: Optional[Callable[[], None]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ops = ops
        self._collector = collector
        self._items = {item.handle: item for item in items}
        self._context = context
        self._user_id = user_id
        self._deadline = deadline
        self._on_still_running = on_still_running
        self._clock = clock
        self._order = asyncio.Lock()
        self._ran_on = False   # a change answered still-running: later changes of this run do not start
        self._gated = content_gate_applies(context)   # not a chat turn: changes stop once passages were read (A14)
        self._passages_step: Optional[int] = None   # the first step whose passages text reached the model
        self.step = 0

    def left(self) -> float:
        return self._deadline - self._clock()

    @property
    def handlers(self) -> Dict[str, LocalToolHandler]:
        return {
            tools.SEARCH_DRIVE: self._search, tools.SEARCH_DELETED: self._search_deleted,
            tools.LIST_FOLDER: self._list, tools.OPEN_DRIVE_FILE: self._open, tools.FIND_PASSAGES: self._find_passages,
            tools.READ_PASSAGES: self._read_passages, tools.GET_RECORDING_LINK: self._link,
            tools.COPY_FILE_TO_DRIVE: self._copy, tools.SAVE_TEXT_FILE: self._save_text,
            tools.CREATE_FOLDER: self._create_folder, tools.MOVE_FILE: self._move,
            tools.APPEND_TO_FILE: self._append, tools.REPLACE_FILE_CONTENT: self._replace,
            tools.DELETE_DRIVE_ITEM: self._delete,
        }

    # -- running one call ------------------------------------------------------------------------------------------

    async def run(self, op: DriveOp, kind: str, call: _Call, *, change: bool, ordered: Optional[bool] = None,
                  running_text: str = STILL_RUNNING,
                  prep: Optional[Callable[[], Awaitable[None]]] = None) -> CollectedResult:
        """The call's collected result. `step` is read before any await: the call belongs to the turn that emitted
        it. `ordered` (default: `change`) takes the per-execution ordering lock, so the call runs in emission order
        with the batch's changes: `get_note` is a read that takes it (specialist RFC §9.2). `running_text` is what a
        change cancelled after it started is recorded with (the note tools pass their own, design Δ10). `prep`
        (changes only) is a read the call needs first, e.g. a destination folder's lookup: it runs inside the order
        lock but before `started`, so a cut during it is "not started", never "still running". A change after the
        step that read passages is refused on a run that is not a chat turn (§28.3 A14)."""
        step = self.step
        if change and write_refused(gated=self._gated, step=step, passages_step=self._passages_step):
            logger.info("[DriveAgent] %s refused: passages were read in step %s of this run", kind, self._passages_step)
            return self._collector.add(step, kind, DriveOutcome(
                op=op, cls=OutcomeClass.REFUSED, text=t.WRITE_AFTER_PASSAGES, success=False))
        if self.left() <= 0:
            return self._collector.add(step, kind, _not_finished(op, t.NOT_STARTED))
        started = False
        try:
            if change:
                async with self._order:
                    if self._ran_on:   # queued behind a change that is still running
                        return self._collector.add(step, kind, _not_finished(op, t.NOT_STARTED_BEHIND))
                    if self.left() <= 0:   # queued behind the batch past the deadline
                        return self._collector.add(step, kind, _not_finished(op, t.NOT_STARTED))
                    if prep is not None:
                        await prep()
                    started = True
                    outcome = await call()
                    if outcome.cls is OutcomeClass.STILL_RUNNING:
                        self._ran_on = True
            elif ordered:
                async with self._order:
                    if self.left() <= 0:   # queued behind the batch past the deadline
                        return self._collector.add(step, kind, _not_finished(op, t.NOT_STARTED))
                    outcome = await asyncio.wait_for(call(), timeout=max(0.001, self.left()))
            else:
                outcome = await asyncio.wait_for(call(), timeout=max(0.001, self.left()))
        except asyncio.TimeoutError:
            outcome = _not_finished(op, t.DID_NOT_FINISH)
        except asyncio.CancelledError:
            # Cancelled while queued behind the batch's order lock: it never started. A change cancelled once started
            # runs on under its shield (DriveOperations' arms the late-failure notice; a note write's posts its
            # receipt late): still running, never "not done" (a re-commission would repeat an append). A read just
            # did not finish.
            if change and started:
                self._collector.add(step, kind, DriveOutcome(
                    op=op, cls=OutcomeClass.STILL_RUNNING, text=running_text, success=True))
            else:
                self._collector.add(step, kind, _not_finished(
                    op, (t.NOT_STARTED_BEHIND if self._ran_on else t.NOT_STARTED) if change else t.DID_NOT_FINISH))
            raise
        result = self._collector.add(step, kind, outcome)
        if outcome.cls is OutcomeClass.STILL_RUNNING and self._on_still_running is not None:
            self._on_still_running()
        return result

    _run = run   # the drive tools' own name for it

    def record(self, op: DriveOp, kind: str, outcome: DriveOutcome) -> CollectedResult:
        """Record an outcome decided before any service call (a refusal of the request's shape), in this step."""
        return self._collector.add(self.step, kind, outcome)

    def _wait_s(self) -> float:
        return max(MUTATION_WAIT_MIN_S, min(MUTATION_WAIT_MAX_S, self.left() - MUTATION_WAIT_MARGIN_S))

    @staticmethod
    def _answer(tc: ToolCall, text: str) -> ToolResult:
        return ToolResult(name=tc.name, result_str=text)

    def _seen(self, tc: ToolCall, result: CollectedResult) -> ToolResult:
        """What the specialist's model reads: the full text, or a stand-in for a body or a file (§6)."""
        outcome = result.outcome
        label = drive_label(outcome.items[0]) if outcome.items else str((tc.args or {}).get("file_ref") or "")
        if outcome.file_data:
            return self._answer(tc, t.STAND_IN_FILE.format(handle=result.handle, label=label,
                                                           mime=outcome.file_data.get("mime_type", "")))
        if outcome.facts.too_long:
            # the refusal's text names no tool; this caller appends its own route (RFC §29.6 L13)
            ref = outcome.items[0].ref if outcome.items else str((tc.args or {}).get("file_ref") or "")
            return self._answer(tc, f"[{result.handle}]\n{outcome.text}\n" + ROUTE_SPECIALIST.format(ref=ref))
        info = outcome.open_bound
        if outcome.body and outcome.facts.cut and info is not None and info.kept:
            ref = outcome.items[0].ref if outcome.items else ""
            return self._answer(tc, stand_in_cut(result.handle, label,
                                                 BoundText("", info.chars, info.kept, info.cut_state), ref, info.extent))
        if outcome.body:
            return self._answer(tc, t.STAND_IN_TEXT.format(handle=result.handle, label=label,
                                                           chars=len(outcome.text)))
        return self._answer(tc, f"[{result.handle}]\n{outcome.text}")

    # -- reads -----------------------------------------------------------------------------------------------------

    async def _search(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        key = search_key(args)
        earlier = self._collector.earlier_search(key)
        if earlier is not None:
            self._collector.repeats_skipped += 1
            logger.info("[DriveAgent] identical search skipped (same as %s)", earlier)
            return self._answer(tc, t.SAME_SEARCH.format(handle=earlier))
        result = await self._run(DriveOp.SEARCH, t.KIND_SEARCH, lambda: self._ops.search(
            self._context, self._user_id, query=args.get("query"), folder=args.get("folder"), kind=args.get("kind"),
            date_from=args.get("date_from"), date_to=args.get("date_to")), change=False)
        if result.outcome.cls not in (OutcomeClass.REQUEST_ERROR, OutcomeClass.NOT_FINISHED):
            self._collector.remember_search(key, result.handle)
        return self._seen(tc, result)

    async def _search_deleted(self, tc: ToolCall) -> ToolResult:
        query = (tc.args or {}).get("query")
        result = await self._run(DriveOp.SEARCH, t.KIND_DELETED_SEARCH, lambda: self._ops.search_deleted(
            self._context, self._user_id, query), change=False)
        if result.outcome.stored_text_shown:   # transcript passages of deleted files: content the model read (A14)
            self._arm_gate(result)
        return self._seen(tc, result)

    async def _list(self, tc: ToolCall) -> ToolResult:
        folder = (tc.args or {}).get("folder")
        result = await self._run(DriveOp.LIST, t.KIND_LISTING, lambda: self._ops.list_folder(
            self._context, self._user_id, folder), change=False)
        return self._seen(tc, result)

    async def _open(self, tc: ToolCall) -> ToolResult:
        ref = str((tc.args or {}).get("file_ref") or "")
        if not ref.strip():
            return self._no_ref(tc, DriveOp.OPEN, "file_ref", "the file to open")
        if _is_bare_path(ref):
            return self._path_given(tc, DriveOp.OPEN, "file_ref", "the file to open")
        result = await self._run(DriveOp.OPEN, t.KIND_OPEN, lambda: self._ops.open(
            self._context, self._user_id, ref), change=False)
        return self._seen(tc, result)

    async def _link(self, tc: ToolCall) -> ToolResult:
        ref = str((tc.args or {}).get("file_ref") or "")
        if not ref.strip():
            return self._no_ref(tc, DriveOp.OPEN, "file_ref", "the recording")
        result = await self._run(DriveOp.OPEN, t.KIND_LINK, lambda: self._ops.open(
            self._context, self._user_id, ref, original=True), change=False)
        return self._seen(tc, result)

    async def _find_passages(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        ledger = self._collector.ledger
        result = await self._run(DriveOp.PASSAGES, t.KIND_PASSAGES, lambda: self._ops.find_passages(
            self._context, self._user_id, question=args.get("question"), folder=args.get("folder"),
            file_refs=args.get("file_refs"), shown=ledger.keys(), shown_folders=ledger.folders_shown(),
            described_files=ledger.files_described()), change=False)
        return self._passages_seen(tc, result)

    async def _read_passages(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        ledger = self._collector.ledger
        result = await self._run(DriveOp.PASSAGES, t.KIND_PASSAGES, lambda: self._ops.read_passages(
            self._context, self._user_id, file_ref=args.get("file_ref"), passages=args.get("passages"),
            shown_folders=ledger.folders_shown(), described_files=ledger.files_described()), change=False)
        return self._passages_seen(tc, result)

    def _passages_seen(self, tc: ToolCall, result: CollectedResult) -> ToolResult:
        """The model reads the passages in full; from this step on the run has read file content (A14)."""
        excerpts = result.outcome.excerpts
        if excerpts is not None and excerpts.files:
            self._arm_gate(result)
        return self._seen(tc, result)

    def _arm_gate(self, result: CollectedResult) -> None:
        self._passages_step = result.step if self._passages_step is None else min(self._passages_step, result.step)

    # -- changes ---------------------------------------------------------------------------------------------------

    async def _folder(self, folder: Any) -> Any:
        return await self._ops.folder_path(self._user_id, folder) if folder else folder

    async def _change(self, tc: ToolCall, op: DriveOp, call: Callable[[Any], Awaitable[DriveOutcome]],
                      folder: Any = None) -> ToolResult:
        """`call` receives the destination folder resolved by `_folder`, which runs before the run counts as started."""
        resolved: list = []

        async def prep() -> None:
            resolved.append(await self._folder(folder))

        async def started_call() -> DriveOutcome:
            return await call(resolved[0])
        return self._seen(tc, await self._run(op, op.value, started_call, change=True, prep=prep))

    def _refuse(self, tc: ToolCall, op: DriveOp, text: str, cls: OutcomeClass) -> ToolResult:
        """A refusal decided before any service call (an unknown item, a request shape), recorded like an outcome."""
        result = self._collector.add(self.step, op.value, DriveOutcome(op=op, cls=cls, text=text, success=False))
        return self._seen(tc, result)

    def _item(self, value: Any) -> Optional[FileContentItem]:
        return self._items.get(str(value or "").strip())

    def _no_item(self, tc: ToolCall, op: DriveOp, value: Any) -> ToolResult:
        text = t.NO_ITEM.format(value=str(value or ""), known=", ".join(self._items) or t.NO_ITEMS)
        return self._refuse(tc, op, text, OutcomeClass.NOT_POSSIBLE)

    def _no_ref(self, tc: ToolCall, op: DriveOp, field: str, what: str) -> ToolResult:
        """An empty ref means "not given": refused before any call, correctable (§7.2)."""
        return self._refuse(tc, op, t.REF_REQUIRED.format(field=field, what=what), OutcomeClass.REQUEST_ERROR)

    def _path_given(self, tc: ToolCall, op: DriveOp, field: str, what: str) -> ToolResult:
        """A path where a `drive:` ref is needed: correctable, and not the "chat file" hint (X2, X3)."""
        return self._refuse(tc, op, t.REF_REQUIRED.format(field=field, what=f"{what} by its drive: ref; {t.FIND_REF}"),
                            OutcomeClass.REQUEST_ERROR)

    async def _copy(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        source = str(args.get("source_ref") or "")
        if not source.strip():
            return self._no_ref(tc, DriveOp.COPY, "source_ref", "the file to keep")

        async def call(folder: Any) -> DriveOutcome:
            return await self._ops.copy(self._context, self._user_id, source, folder=folder,
                                        file_name=args.get("file_name"), wait_s=self._wait_s())
        return await self._change(tc, DriveOp.COPY, call, args.get("folder"))

    async def _save_text(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        item = self._item(args.get("text_item"))
        if item is None:
            return self._no_item(tc, DriveOp.SAVE_TEXT, args.get("text_item"))

        async def call(folder: Any) -> DriveOutcome:
            return await self._ops.save_text(self._context, self._user_id, item.text,
                                             file_name=args.get("file_name") or item.name,
                                             folder=folder, wait_s=self._wait_s())
        return await self._change(tc, DriveOp.SAVE_TEXT, call, args.get("folder"))

    async def _create_folder(self, tc: ToolCall) -> ToolResult:
        folder = (tc.args or {}).get("folder")

        async def call(resolved: Any) -> DriveOutcome:
            return await self._ops.create_folder(self._context, self._user_id, resolved, wait_s=self._wait_s())
        return await self._change(tc, DriveOp.CREATE_FOLDER, call, folder)

    async def _move(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        folder, new_name = args.get("folder"), args.get("new_name")
        if not (isinstance(folder, str) and folder.strip()) and not (isinstance(new_name, str) and new_name.strip()):
            # the service's own words, before any call: a correctable request shape (§7.2)
            return self._refuse(tc, DriveOp.MOVE, MOVE_NEEDS_TARGET, OutcomeClass.REQUEST_ERROR)
        ref = str(args.get("file_ref") or "")
        if _is_bare_path(ref):
            return self._path_given(tc, DriveOp.MOVE, "file_ref", "the file to move")

        async def call(resolved: Any) -> DriveOutcome:
            return await self._ops.move(self._context, self._user_id, ref, folder=resolved,
                                        new_name=new_name, wait_s=self._wait_s())
        return await self._change(tc, DriveOp.MOVE, call, folder)

    async def _append(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        item = self._item(args.get("text_item"))
        if item is None:
            return self._no_item(tc, DriveOp.APPEND, args.get("text_item"))
        ref = str(args.get("file_ref") or item.target or "")
        if _is_bare_path(ref):
            return self._path_given(tc, DriveOp.APPEND, "file_ref", "the text file to add to")

        async def call(_folder: Any) -> DriveOutcome:
            return await self._ops.append(self._context, self._user_id, ref, item.text, wait_s=self._wait_s())
        return await self._change(tc, DriveOp.APPEND, call)

    async def _replace(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        target, source = str(args.get("target_ref") or ""), str(args.get("new_content_ref") or "")
        if _is_bare_path(target):
            return self._path_given(tc, DriveOp.REPLACE, "target_ref", "the file to replace")

        async def call(_folder: Any) -> DriveOutcome:
            return await self._ops.replace(self._context, self._user_id, target, source, wait_s=self._wait_s())
        return await self._change(tc, DriveOp.REPLACE, call)

    async def _delete(self, tc: ToolCall) -> ToolResult:
        ref = str((tc.args or {}).get("file_ref") or "")
        if _is_bare_path(ref):
            return self._path_given(tc, DriveOp.DELETE, "file_ref", "the item to delete")

        async def call(_folder: Any) -> DriveOutcome:
            return await self._ops.delete(self._context, self._user_id, ref, wait_s=self._wait_s())
        return await self._change(tc, DriveOp.DELETE, call)


def _is_bare_path(ref: str) -> bool:
    """A path where a ref is needed: it has a `/` and no known store prefix (`drive:`, `workspace:`, `skill:`, a
    delivered-document key). A bare name has no `/` and keeps today's text (X2, X3)."""
    if "/" not in ref or is_drive_ref(ref):
        return False
    return not ref.startswith((WORKSPACE_REF_PREFIX, SKILL_REF_PREFIX) + tuple(DELIVERED_REF_PREFIXES))


def _not_finished(op: DriveOp, text: str) -> DriveOutcome:
    return DriveOutcome(op=op, cls=OutcomeClass.NOT_FINISHED, text=text, success=False)
