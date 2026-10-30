"""
The drive evals' note-write stub (DRIVE_SPECIALIST_RFC §12 "Harness safety first", §9): while installed, every write of
every `DriveNoteService` instance answers a receipt and never runs; every read runs as written; each call is logged
beside the drive operations' (`DriveOpLog`, op names prefixed `note_`).

Why it exists: `DryRunDriveOperations` stubs only `DriveOperations.MUTATION_METHODS`, but the specialist's note tools
reach the owner's notes through `DriveNoteService`, not through `DriveOperations`. A G3 or specialist-eval run builds the
real factory, whose service holds the LIVE note store, so without this class a scripted or model-chosen `create_note`
would write the owner's notes. Patching the class covers every instance in the process, the factory's and any built
later. Installing refuses a `DriveNoteService` with a public coroutine that is in neither READ_METHODS nor WRITE_METHODS:
a new method is classified before any dry run can call it.

    log = DriveOpLog()
    with DryRunDriveNotes(log):
        ...   # build agents, run cases
"""
from __future__ import annotations

from typing import Any, Callable, Dict, FrozenSet, Optional, Type

from .dry_run_drive import DriveOpLog, named_args, public_coroutines
from src.domain.drive_note import (
    ERASE_SCOPE_ALL, ERASE_SCOPE_ONE, EraseRequestReceipt, NoteContent, NoteReceipt)
from src.services.drive_note_service import DriveNoteService

DRY_PATH = "(dry run)"
DRY_NOTE_ID = "dry-run-note"
DRY_CODE = "dryrun"


class DryRunNotesError(RuntimeError):
    """The note dry run cannot be installed safely."""


def _receipt(action: str, ref: str, text: str = "") -> NoteReceipt:
    return NoteReceipt(action=action, note_id=DRY_NOTE_ID, ref=ref, path=DRY_PATH, version=1, text=text,
                       state="active")


def _set(args: Dict[str, Any]) -> Any:
    return _receipt("set", args["ref"], str(args.get("note") or ""))


def _create(args: Dict[str, Any]) -> Any:
    content = args.get("content")
    return _receipt("create", args["ref"], content.text if isinstance(content, NoteContent) else "")


def _update(args: Dict[str, Any]) -> Any:
    return _receipt("update", args["ref"])


def _clear(args: Dict[str, Any]) -> Any:
    return _receipt("clear", args["ref"])


def _undo(args: Dict[str, Any]) -> Any:
    return _receipt("restore", args["ref"])


def _request_erase(args: Dict[str, Any]) -> Any:
    return EraseRequestReceipt(scope=ERASE_SCOPE_ONE, code=DRY_CODE, note_count=1, path=DRY_PATH, version_count=1)


def _request_erase_all(args: Dict[str, Any]) -> Any:
    return EraseRequestReceipt(scope=ERASE_SCOPE_ALL, code=DRY_CODE, note_count=1)


def _erase(args: Dict[str, Any]) -> Any:
    return 0


# The stubbed answer of each write: a receipt shaped like the real one, naming nothing real.
STUBS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "set": _set, "create": _create, "update": _update, "clear": _clear, "undo": _undo,
    "request_erase": _request_erase, "request_erase_all": _request_erase_all, "erase": _erase,
}
WRITE_METHODS: FrozenSet[str] = frozenset(STUBS)
READ_METHODS: FrozenSet[str] = frozenset({"get", "note_for_item"})
_NOT_LOGGED_ARGS = frozenset({"provenance"})
_LOGGED_ARGS = ("ref", "doc_type", "life_areas", "code", "expected_version")


class DryRunDriveNotes:
    """Install with `with DryRunDriveNotes(log):` (or install()/uninstall()); restores the class on exit."""

    def __init__(self, log: DriveOpLog, cls: Type[Any] = DriveNoteService) -> None:
        self.log = log
        self._cls = cls
        self._saved: Dict[str, Callable[..., Any]] = {}

    def install(self) -> None:
        if self._saved:
            raise DryRunNotesError("the note dry run is already installed")
        unclassified = sorted(set(public_coroutines(self._cls)) - READ_METHODS - WRITE_METHODS)
        if unclassified:
            raise DryRunNotesError(f"DriveNoteService methods not classified as read or write: {unclassified}")
        for name in sorted(WRITE_METHODS):
            original = getattr(self._cls, name)
            self._saved[name] = original
            setattr(self._cls, name, self._stub(name, original))

    def uninstall(self) -> None:
        for name, original in self._saved.items():
            setattr(self._cls, name, original)
        self._saved.clear()

    def __enter__(self) -> "DryRunDriveNotes":
        self.install()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()

    def _stub(self, name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        log, answer = self.log, STUBS[name]

        async def stubbed(service: Any, *args: Any, **kwargs: Any) -> Any:
            named = named_args(original, service, args, kwargs)
            # The log names the target and the classifiers, never the note's words (the repo is public, the log local).
            logged: Dict[str, Optional[Any]] = {k: named[k] for k in _LOGGED_ARGS if k in named}
            log.add(f"note_{name}", logged, outcome="ok", stubbed=True)
            return answer(named)
        return stubbed
