"""
The drive evals' write stub (DRIVE_SPECIALIST_RFC §12 "Harness safety first"): while installed, every mutation of
every DriveOperations instance answers "accepted" and never runs; every read runs as written; each call is logged
with its method, its named arguments and its outcome class.

Why at the class, not on one instance: G3 (`smart_dry_run`) and the specialist eval build the real Smart with the
real factory, and the specialist's tools reach the drive only through DriveOperations, never through
`DelegationEngine.dispatch` (where today's harness stubs writes per intent). Patching the class, as G3 patches
`DelegationEngine.dispatch`, covers every instance in the process — the fixture's and any the factory builds — so no
wiring path can write to the owner's OneDrive. Installing refuses a DriveOperations with a public coroutine that is in
neither READ_METHODS nor MUTATION_METHODS (part A's classification): a new method is classified before any dry run
can call it.

DriveOperations' methods take `(context, user_id, <ref or text>, *, <keywords>)`; a log entry names every argument
through the method's own signature, without `context`, `user_id` and a mutation's `wait_s` (the caller's, not the
operation's input). A read that returns no DriveOutcome (`preflight`, `folder_path`) is logged as "ok".

    log = DriveOpLog()
    with DryRunDriveOperations(log):
        ...   # build agents, run cases
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.infrastructure.drive_operations import DriveOperations

STUB_TEXT = "Done: '{op}' accepted and completed successfully."   # G3's STUB_RESULT wording, per operation
_NOT_LOGGED = frozenset({"self", "context", "user_id", "wait_s"})


class DryRunError(RuntimeError):
    """The dry run cannot be installed safely."""


@dataclass
class DriveOpLog:
    """Every DriveOperations call in a case, in call order: reads as they ran, mutations as they were stubbed."""
    entries: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, method: str, args: Dict[str, Any], *, outcome: str, stubbed: bool, **extra: Any) -> None:
        self.entries.append({"op": method, "args": args, "outcome": outcome, "stubbed": stubbed, **extra})

    def clear(self) -> None:
        self.entries.clear()


def public_coroutines(cls: Type[Any]) -> List[str]:
    return sorted(name for name, member in inspect.getmembers(cls)
                  if not name.startswith("_") and inspect.iscoroutinefunction(member))


def named_args(original: Callable[..., Any], ops: Any, args: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """The call's arguments by parameter name (a positional ref, source or text is named too)."""
    bound = inspect.signature(original).bind(ops, *args, **kwargs)
    return {name: value for name, value in bound.arguments.items() if name not in _NOT_LOGGED}


class DryRunDriveOperations:
    """Install with `with DryRunDriveOperations(log):` (or install()/uninstall()); restores the class on exit."""

    def __init__(self, log: DriveOpLog, cls: Type[Any] = DriveOperations,
                 annotate: Optional[Callable[[str, DriveOutcome], Dict[str, Any]]] = None) -> None:
        """`annotate(method, outcome)` adds fields to a read's log entry (G3 marks a search whose text names a word no
        file holds)."""
        self.log = log
        self._cls = cls
        self._annotate = annotate
        self._saved: Dict[str, Callable[..., Any]] = {}

    def install(self) -> None:
        if self._saved:
            raise DryRunError("the dry run is already installed")
        reads, writes = set(self._cls.READ_METHODS), set(self._cls.MUTATION_METHODS)
        unclassified = sorted(set(public_coroutines(self._cls)) - reads - writes)
        if unclassified or reads & writes:
            raise DryRunError(f"DriveOperations methods not classified as read or mutation: {unclassified}; "
                              f"in both: {sorted(reads & writes)}")
        for name in sorted(reads | writes):
            original = getattr(self._cls, name)
            self._saved[name] = original
            setattr(self._cls, name, self._stub(name, original) if name in writes else self._logged(name, original))

    def uninstall(self) -> None:
        for name, original in self._saved.items():
            setattr(self._cls, name, original)
        self._saved.clear()

    def __enter__(self) -> "DryRunDriveOperations":
        self.install()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()

    def _stub(self, name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        log, op = self.log, self._cls.OP_OF[name]

        async def stubbed(ops: Any, *args: Any, **kwargs: Any) -> DriveOutcome:
            log.add(name, named_args(original, ops, args, kwargs), outcome=OutcomeClass.OK.value, stubbed=True)
            return DriveOutcome(op=op, cls=OutcomeClass.OK, text=STUB_TEXT.format(op=name), success=True)
        return stubbed

    def _logged(self, name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        log, annotate = self.log, self._annotate

        async def read(ops: Any, *args: Any, **kwargs: Any) -> Any:
            result: Any = None
            done = False
            try:
                result = await original(ops, *args, **kwargs)
                done = True
                return result
            finally:
                if isinstance(result, DriveOutcome):
                    extra = annotate(name, result) if annotate is not None else {}
                    log.add(name, named_args(original, ops, args, kwargs), outcome=result.cls.value, stubbed=False,
                            **extra)
                else:
                    log.add(name, named_args(original, ops, args, kwargs), outcome="ok" if done else "raised",
                            stubbed=False)
        return read
