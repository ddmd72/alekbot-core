"""The drive specialist's note tools, served locally by its DelegationEngine (DRIVE_SPECIALIST_RFC §9.1, §9.2, §9.3 A4;
design §4.3, Δ7, Δ8, Δ10; DRIVE_FILE_NOTES_RFC §4.5, §4.6, §4.8).

Built per execution beside the drive tools (`DriveToolRun`), on the same run: every call goes through
`DriveToolRun.run`, so it is recorded in the execution's pass-through collector, numbered by its step and bound by the
soft deadline, and every call takes the run's ordering lock (`get_note` too), so a batch applies in emission order: a
`get_note` emitted before a `replace_note_text` runs first.

Each handler makes exactly one `DriveNoteService` call: `get_note` → `get`; `create_note` → `create`; `add_to_note`,
`replace_note_text`, `set_note_type`, `clear_note_type`, `add_life_areas`, `remove_life_areas` → `update` with one
`NoteChange`; `delete_note` → `clear`; `undo_note` → `undo`; `erase_note` → `request_erase`.

Gates, before any service call: the truncated-JSON refusal; a writer re-checks `is_owner_turn(context)` (the run's
`message.context`, never the model's payload; the writers are declared only on an owner turn, §9.1); an empty
`item_ref` or an empty change is refused (an empty field never writes); then, under the ordering lock,
NOTE_WRITES_PER_TURN successful writes per execution (refusals do not count) and, for `replace_note_text`, a
`get_note` read of that note earlier in this execution (its version is the write's precondition).

A write runs in a task held by the per-user `DriveOperations.keep_alive` and awaited under `asyncio.shield`: a run cut
mid-write (the soft deadline) still finishes it; the call is recorded as still running with NOTE_STILL_RUNNING, and
when the write lands its receipt is posted late through `DriveOperations.post_late_note_receipt` (Δ10). A write that
lands while its call is awaited records its receipt and history line in the outcome, so they reach the answer from the
collector only (Δ7): exactly one receipt either way.

No result carries the note's words except `get_note`'s, wrapped as untrusted data (Δ8); the erase code rides only in
the erase request's `DeliveryItem.data`. Logs carry ids, counts and reasons, never the text.

Public interface:
    DRIVE_NOTE_CONTEXT_KEY
    note_provenance(context) -> NoteProvenance
    DriveNoteSession                  one execution's write count and read set
    DriveNoteToolRun(run, service, ops, context, user_id, account_id, provenance, *, writes, session=None)
        .handlers -> Dict[str, LocalToolHandler]     get_note always; the ten writers only when `writes`
"""
from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Mapping, Optional

from ..domain import drive_answer_texts as t
from ..domain import drive_note_tool_texts as nt
from ..domain.agent import DeliveryItem
from ..domain.drive_collected_result import CollectedResult
from ..domain.drive_index import DriveIndexStoreUnavailable
from ..domain.drive_note import (
    DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY, NOTE_WRITES_PER_TURN, OWNER_TURN_RUN_ORIGINS,
    DriveNoteRefused, EraseRequestReceipt, NoteOrigin, NoteProvenance, NoteReceipt, NoteRefusal,
    drive_note_context_entry, is_owner_turn, note_content,
)
from ..domain.drive_note_change import NoteChange
from ..domain.drive_note_view import NoteView
from ..domain.drive_op import DriveOp
from ..domain.drive_outcome import DriveOutcome
from ..domain.drive_outcome_class import OutcomeClass
from ..domain.drive_read_facts import ReadFacts
from ..domain.llm import ToolCall
from ..domain.turn_identity import TURN_ID_CONTEXT_KEY
from ..utils.logger import logger
from .delegation_engine import LocalToolHandler, ToolResult

if TYPE_CHECKING:
    from ..services.drive_note_service import DriveNoteService
    from .drive_operations import DriveOperations
    from .drive_tools import DriveToolRun

DRIVE_NOTE_CONTEXT_KEY = "drive_note_context"

# The class of each refusal (design §4.3): correctable in the loop, a hard refusal, or a failure. Anything not listed
# (CONFLICT, DRIVE_UNAVAILABLE, HISTORY_MISSING, ERASE_UNAVAILABLE, …) is FAILED.
_REFUSAL_CLASS: Dict[NoteRefusal, OutcomeClass] = {
    NoteRefusal.NOT_CONNECTED: OutcomeClass.NOT_CONNECTED,
    NoteRefusal.NOT_ON_DRIVE: OutcomeClass.NOT_FOUND,
    NoteRefusal.NO_NOTE: OutcomeClass.NOT_FOUND,
    NoteRefusal.CANNOT_OPEN: OutcomeClass.REFUSED,
    NoteRefusal.SECURITY_REJECTED: OutcomeClass.REFUSED,
    NoteRefusal.NOTE_LIMIT: OutcomeClass.REFUSED,
    NoteRefusal.TOO_LONG: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.UNKNOWN_DOC_TYPE: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.AMBIGUOUS_DOC_TYPE: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.UNKNOWN_LIFE_AREA: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.AMBIGUOUS_LIFE_AREA: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.TOO_MANY_LIFE_AREAS: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.NOTE_EXISTS: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.EMPTY_CHANGE: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.CHANGED_SINCE_READ: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.INVALID_REF: OutcomeClass.REQUEST_ERROR,
    NoteRefusal.AMBIGUOUS_NOTE: OutcomeClass.REQUEST_ERROR,
}

_Service = Callable[[], Awaitable[Any]]


def note_provenance(context: Mapping[str, Any]) -> NoteProvenance:
    """Which owner turn writes (DRIVE_FILE_NOTES_RFC §3.2, §4.9): `voice` on a call with Lelik (`run_origin` one of
    OWNER_TURN_RUN_ORIGINS), else `chat`; the session; and the turn id the entry point fixed before Smart ran
    (TURN_ID_CONTEXT_KEY)."""
    origin = context.get("run_origin")
    voice = isinstance(origin, str) and origin in OWNER_TURN_RUN_ORIGINS
    return NoteProvenance(origin=(NoteOrigin.VOICE if voice else NoteOrigin.CHAT).value,
                          session_id=str(context.get("session_id") or ""),
                          turn_id=str(context.get(TURN_ID_CONTEXT_KEY) or ""))


class DriveNoteSession:
    """One execution's note state: the writes that succeeded and the notes `get_note` read, each with the version it
    read (0: no note). Per execution, never shared; touched only on the event loop."""

    def __init__(self) -> None:
        self.writes = 0
        self.reads: Dict[str, int] = {}

    def remember(self, ref: str, view: Optional[NoteView]) -> None:
        """Record a read under every alias of the note: the ref given, its `drive:` ref and its `drive_note:` ref."""
        version = view.version if view is not None else 0
        aliases = [ref] + ([view.ref, view.note_ref] if view is not None else [])
        for alias in aliases:
            if alias:
                self.reads[alias] = version

    def read_version(self, ref: str) -> Optional[int]:
        return self.reads.get(ref)


def _areas(value: Any) -> List[str]:
    """Liberal: a list of names, or one string of comma-separated names."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _text(args: Mapping[str, Any], name: str) -> str:
    """Liberal: a string as given, a number as its digits; anything else (absent, null, a list) is "not given"."""
    value = args.get(name)
    if isinstance(value, str):
        return value
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else ""


def _failed(op: DriveOp, text: str, cls: OutcomeClass) -> DriveOutcome:
    return DriveOutcome(op=op, cls=cls, text=text, success=False)


class DriveNoteToolRun:
    """One execution's note tool handlers, on the drive tools' run (its collector, step, deadline and ordering lock)."""

    def __init__(self, run: "DriveToolRun", service: "DriveNoteService", ops: "DriveOperations",
                 context: Mapping[str, Any], user_id: str, account_id: str, provenance: NoteProvenance, *,
                 writes: bool, session: Optional[DriveNoteSession] = None,
                 new_nonce: Callable[[], str] = lambda: secrets.token_hex(8)) -> None:
        """`context` is the execution's `message.context` (the owner-turn gate reads it, never the payload);
        `provenance` comes from `note_provenance(context)`; `writes` declares the ten writers (an owner turn).
        `session` is injectable for tests only."""
        self._run = run
        self._service = service
        self._ops = ops
        self._context = context
        self._user_id = user_id
        self._account_id = account_id
        self._provenance = provenance
        self._writes = writes
        self._session = session if session is not None else DriveNoteSession()
        self._new_nonce = new_nonce

    @property
    def handlers(self) -> Dict[str, LocalToolHandler]:
        handlers: Dict[str, LocalToolHandler] = {nt.GET_NOTE: self._get}
        if self._writes:
            handlers.update({
                nt.CREATE_NOTE: self._create, nt.ADD_TO_NOTE: self._add, nt.REPLACE_NOTE_TEXT: self._replace,
                nt.SET_NOTE_TYPE: self._set_type, nt.CLEAR_NOTE_TYPE: self._clear_type,
                nt.ADD_LIFE_AREAS: self._add_areas, nt.REMOVE_LIFE_AREAS: self._remove_areas,
                nt.DELETE_NOTE: self._delete, nt.UNDO_NOTE: self._undo, nt.ERASE_NOTE: self._erase,
            })
        return handlers

    # -- the read ----------------------------------------------------------------------------------------------------

    async def _get(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        early = self._precheck(tc, DriveOp.NOTE_READ, args, writer=False)
        if early is not None:
            return early
        ref = _text(args, "item_ref").strip()

        async def call() -> DriveOutcome:
            try:
                view = await self._service.get(self._user_id, ref)
            except Exception as exc:
                return self._error(DriveOp.NOTE_READ, tc.name, exc)
            self._session.remember(ref, view)
            if view is None:
                return _failed(DriveOp.NOTE_READ, nt.NO_NOTE_READ.format(ref=ref), OutcomeClass.NOT_FOUND)
            return DriveOutcome(op=DriveOp.NOTE_READ, cls=OutcomeClass.OK, success=True,
                                text=nt.note_view_text(view, self._new_nonce()),
                                facts=ReadFacts(owner_note=not view.cleared))
        return self._seen(tc, await self._run.run(DriveOp.NOTE_READ, t.KIND_NOTE, call, change=False, ordered=True))

    # -- the writers -------------------------------------------------------------------------------------------------

    async def _create(self, tc: ToolCall) -> ToolResult:
        args = tc.args or {}
        early = self._precheck(tc, DriveOp.NOTE_WRITE, args, writer=True)
        if early is not None:
            return early
        try:
            content = note_content(_text(args, "text"), _text(args, "doc_type"), _areas(args.get("life_areas")))
        except DriveNoteRefused as exc:
            return self._early_refusal(tc, DriveOp.NOTE_WRITE, exc)
        if content.is_empty:
            return self._early(tc, DriveOp.NOTE_WRITE, nt.EMPTY_CREATE, OutcomeClass.REQUEST_ERROR, reason="empty")
        return await self._write(tc, DriveOp.NOTE_WRITE, args, lambda ref: self._service.create(
            self._user_id, self._account_id, ref, content, self._provenance), checked=True)

    async def _add(self, tc: ToolCall) -> ToolResult:
        return await self._change(tc, lambda args: NoteChange.append_text(_text(args, "text")))

    async def _replace(self, tc: ToolCall) -> ToolResult:
        return await self._change(tc, lambda args: NoteChange.replace_text(_text(args, "text")), read_first=True)

    async def _set_type(self, tc: ToolCall) -> ToolResult:
        return await self._change(tc, lambda args: NoteChange.set_type(_text(args, "doc_type")))

    async def _clear_type(self, tc: ToolCall) -> ToolResult:
        return await self._change(tc, lambda args: NoteChange.clear_type())

    async def _add_areas(self, tc: ToolCall) -> ToolResult:
        return await self._change(tc, lambda args: NoteChange.add_areas(_areas(args.get("life_areas"))))

    async def _remove_areas(self, tc: ToolCall) -> ToolResult:
        return await self._change(tc, lambda args: NoteChange.remove_areas(_areas(args.get("life_areas"))))

    async def _delete(self, tc: ToolCall) -> ToolResult:
        return await self._write(tc, DriveOp.NOTE_WRITE, tc.args or {}, lambda ref: self._service.clear(
            self._user_id, self._account_id, ref, self._provenance))

    async def _undo(self, tc: ToolCall) -> ToolResult:
        return await self._write(tc, DriveOp.NOTE_WRITE, tc.args or {}, lambda ref: self._service.undo(
            self._user_id, self._account_id, ref, self._provenance))

    async def _erase(self, tc: ToolCall) -> ToolResult:
        return await self._write(tc, DriveOp.NOTE_ERASE, tc.args or {}, lambda ref: self._service.request_erase(
            self._user_id, ref))

    async def _change(self, tc: ToolCall, build: Callable[[Mapping[str, Any]], NoteChange], *,
                      read_first: bool = False) -> ToolResult:
        """One `update` call. The change is built (and validated) before any service call: an empty or invalid
        argument is refused with nothing written."""
        args = tc.args or {}
        early = self._precheck(tc, DriveOp.NOTE_WRITE, args, writer=True)
        if early is not None:
            return early
        try:
            change = build(args)
        except DriveNoteRefused as exc:
            return self._early_refusal(tc, DriveOp.NOTE_WRITE, exc)

        def guard(ref: str) -> Optional[str]:
            return nt.READ_FIRST if read_first and self._session.read_version(ref) is None else None

        def update(ref: str) -> Awaitable[Any]:
            expected = self._session.read_version(ref) if read_first else None
            return self._service.update(self._user_id, self._account_id, ref, change, self._provenance,
                                        expected_version=expected)
        return await self._write(tc, DriveOp.NOTE_WRITE, args, update, guard=guard, checked=True)

    async def _write(self, tc: ToolCall, op: DriveOp, args: Mapping[str, Any],
                     service_call: Callable[[str], Awaitable[Any]], *,
                     guard: Optional[Callable[[str], Optional[str]]] = None, checked: bool = False) -> ToolResult:
        """Run one write under the ordering lock: the cap and the guard are decided there (after the batch's earlier
        calls, a `get_note` among them), then the service call, shielded and held by DriveOperations."""
        if not checked:
            early = self._precheck(tc, op, args, writer=True)
            if early is not None:
                return early
        ref = _text(args, "item_ref").strip()

        async def call() -> DriveOutcome:
            if self._session.writes >= NOTE_WRITES_PER_TURN:
                logger.info(f"[DriveNote] tool user={self._user_id[:8]} {tc.name} refused:cap")
                return _failed(op, nt.WRITE_CAP.format(cap=NOTE_WRITES_PER_TURN), OutcomeClass.REFUSED)
            refused = guard(ref) if guard is not None else None
            if refused is not None:
                logger.info(f"[DriveNote] tool user={self._user_id[:8]} {tc.name} refused:read_first")
                return _failed(op, refused, OutcomeClass.REQUEST_ERROR)
            return await self._shielded(op, tc.name, lambda: service_call(ref))
        result = await self._run.run(op, tc.name, call, change=True, ordered=True,
                                     running_text=nt.NOTE_STILL_RUNNING)
        return self._seen(tc, result)

    async def _shielded(self, op: DriveOp, tool: str, service_call: _Service) -> DriveOutcome:
        """The service call in a task the per-user DriveOperations holds, awaited under `asyncio.shield`. When this
        call is cancelled (the run was cut), the write goes on, and its receipt is posted late when it lands (Δ10);
        the run records the call as still running."""
        task = asyncio.ensure_future(self._land(op, tool, service_call))
        self._ops.keep_alive(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            logger.info(f"[DriveNote] tool user={self._user_id[:8]} {tool}: the run was cut; the write goes on")
            task.add_done_callback(self._post_late)
            raise

    async def _land(self, op: DriveOp, tool: str, service_call: _Service) -> DriveOutcome:
        """The service call and its outcome; never raises (a refusal or an error is an outcome). A success counts
        toward the per-execution cap, also when the run that started it was cut."""
        try:
            done = await service_call()
        except Exception as exc:
            return self._error(op, tool, exc)
        self._session.writes += 1
        if isinstance(done, EraseRequestReceipt):
            return DriveOutcome(op=op, cls=OutcomeClass.OK, text=nt.erase_request_line(done), success=True,
                                delivery=(DeliveryItem(type=DRIVE_NOTE_ERASE_DELIVERY, data=done.delivery_data()),))
        receipt: NoteReceipt = done
        return DriveOutcome(
            op=op, cls=OutcomeClass.OK, text=nt.note_receipt_line(receipt), success=True,
            note_context=(drive_note_context_entry(receipt),) if receipt.note_id else (),
            delivery=(DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY, data=receipt.delivery_data()),))

    def _post_late(self, task: "asyncio.Task[DriveOutcome]") -> None:
        """The done-callback of a write whose call was cut: its receipt never reaches the answer, so it is posted now
        (exactly once: the answer is built from the collector, which recorded only the still-running line)."""
        if task.cancelled() or task.exception() is not None:
            return
        call = self._provenance.origin == NoteOrigin.VOICE.value
        channel = None if call else self._context.get("origin_channel_id")
        platform = None if call else self._context.get("origin_platform")
        for item in task.result().delivery:
            self._ops.post_late_note_receipt(self._user_id, self._account_id, item, call=call, channel_id=channel,
                                             platform=platform)

    # -- the shared gates and results --------------------------------------------------------------------------------

    def _precheck(self, tc: ToolCall, op: DriveOp, args: Mapping[str, Any], *, writer: bool) -> Optional[ToolResult]:
        """The refusals decided before any service call: truncated arguments, not an owner turn (a writer), no ref."""
        if args.get("_parse_error") == "truncated_json":
            return self._early(tc, op, nt.TRUNCATED, OutcomeClass.REQUEST_ERROR, reason="truncated")
        if writer and not is_owner_turn(self._context):
            return self._early(tc, op, nt.NOT_OWNER_TURN, OutcomeClass.REFUSED, reason="not_owner_turn")
        if not _text(args, "item_ref").strip():
            return self._early(tc, op, t.REF_REQUIRED.format(field="item_ref", what=nt.ITEM_REF_REQUIRED),
                               OutcomeClass.REQUEST_ERROR, reason="no_ref")
        return None

    def _early(self, tc: ToolCall, op: DriveOp, text: str, cls: OutcomeClass, *, reason: str) -> ToolResult:
        logger.info(f"[DriveNote] tool user={self._user_id[:8]} {tc.name} refused:{reason}")
        kind = t.KIND_NOTE if op is DriveOp.NOTE_READ else tc.name
        return self._seen(tc, self._run.record(op, kind, _failed(op, text, cls)))

    def _early_refusal(self, tc: ToolCall, op: DriveOp, exc: DriveNoteRefused) -> ToolResult:
        """A change or a note refused by its own validation (too long, an unknown type, empty): nothing is called."""
        return self._early(tc, op, f"{exc} {nt.NOTHING_STORED}", _REFUSAL_CLASS.get(exc.reason, OutcomeClass.FAILED),
                           reason=exc.reason.value)

    def _error(self, op: DriveOp, tool: str, exc: Exception) -> DriveOutcome:
        """A service call that raised: a refusal by its reason's class; the store outage and anything else FAILED. The
        log names the reason or the error's type only: a validation error from a stored document carries its values,
        the note's text among them."""
        user = self._user_id[:8]
        if isinstance(exc, DriveNoteRefused):
            logger.info(f"[DriveNote] tool user={user} {tool} refused:{exc.reason.value}")
            return _failed(op, f"{exc} {nt.NOTHING_STORED}", _REFUSAL_CLASS.get(exc.reason, OutcomeClass.FAILED))
        if isinstance(exc, DriveIndexStoreUnavailable):
            logger.warning(f"[DriveNote] tool user={user} {tool}: the note store is unavailable")
            return _failed(op, nt.STORE_DOWN, OutcomeClass.FAILED)
        logger.error(f"❌ [DriveNote] tool user={user} {tool} failed: {type(exc).__name__}")
        return _failed(op, nt.UNEXPECTED, OutcomeClass.FAILED)

    @staticmethod
    def _seen(tc: ToolCall, result: CollectedResult) -> ToolResult:
        """What the specialist's model reads: the call's handle and its text (a note's words only in get_note's
        wrapped block)."""
        return ToolResult(name=tc.name, result_str=f"[{result.handle}]\n{result.outcome.text}")


__all__ = ["DRIVE_NOTE_CONTEXT_KEY", "DriveNoteSession", "DriveNoteToolRun", "note_provenance"]
