"""
The drive evals' write stub (DRIVE_SPECIALIST_RFC §12 "Harness safety first"): while installed, every mutation of
every DriveOperations instance answers "accepted" and never runs; every read runs as written; each call is logged
with its operation, arguments, outcome class and the agent that made it.

Why at the class, not on one instance: G3 (`smart_dry_run`) and the specialist eval build the real Smart with the
real factory, and the specialist's tools reach the drive only through DriveOperations, never through
`DelegationEngine.dispatch` (where today's harness stubs writes per intent). Patching the class, as G3 patches
`DelegationEngine.dispatch`, covers every instance in the process — the fixture's and any the factory builds — so no
wiring path can write to the owner's OneDrive. Installing refuses a DriveOperations with a public coroutine that is in
neither READ_METHODS nor MUTATION_METHODS: a new method is classified before any dry run can call it.

    log = DriveOpLog()
    with DryRunDriveOperations(log):
        ...   # build agents, run cases
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from src.domain.agent import AgentMessage
from src.domain.drive_op import DriveOpOutcome, DriveOutcome
from src.infrastructure.drive_operations import DriveOperations

STUB_TEXT = "Done: '{op}' accepted and completed successfully."   # G3's STUB_RESULT wording, per operation


class DryRunError(RuntimeError):
    """The dry run cannot be installed safely."""


@dataclass
class DriveOpLog:
    """Every DriveOperations call in a case, in call order: reads as they ran, mutations as they were stubbed."""
    entries: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, method: str, args: Dict[str, Any], *, outcome: str, stubbed: bool, agent: str,
            **extra: Any) -> None:
        self.entries.append({"op": method, "args": args, "outcome": outcome, "stubbed": stubbed, "agent": agent,
                             **extra})

    def clear(self) -> None:
        self.entries.clear()


def public_coroutines(cls: Type[Any]) -> List[str]:
    return sorted(name for name, member in inspect.getmembers(cls)
                  if not name.startswith("_") and inspect.iscoroutinefunction(member))


def _args(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k != "wait_s"}   # a mutation's wait is the caller's, not its input


def _agent(message: Any) -> str:
    return str(getattr(message, "recipient", "") or "")


class DryRunDriveOperations:
    """Install with `with DryRunDriveOperations(log):` (or install()/uninstall()); restores the class on exit."""

    def __init__(self, log: DriveOpLog, cls: Type[Any] = DriveOperations,
                 annotate: Optional[Callable[[str, DriveOpOutcome], Dict[str, Any]]] = None) -> None:
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
            setattr(self._cls, name, self._stub(name) if name in writes else self._logged(name, original))

    def uninstall(self) -> None:
        for name, original in self._saved.items():
            setattr(self._cls, name, original)
        self._saved.clear()

    def __enter__(self) -> "DryRunDriveOperations":
        self.install()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()

    def _stub(self, name: str) -> Callable[..., Any]:
        log, op = self.log, self._cls.OP_OF[name]

        async def stubbed(ops: Any, message: AgentMessage, **kwargs: Any) -> DriveOpOutcome:
            log.add(name, _args(kwargs), outcome=DriveOutcome.OK.value, stubbed=True, agent=_agent(message))
            return DriveOpOutcome(op, DriveOutcome.OK, STUB_TEXT.format(op=name))
        return stubbed

    def _logged(self, name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        log, annotate = self.log, self._annotate

        async def read(ops: Any, message: AgentMessage, **kwargs: Any) -> DriveOpOutcome:
            outcome: Optional[DriveOpOutcome] = None
            try:
                outcome = await original(ops, message, **kwargs)
                return outcome
            finally:
                extra = annotate(name, outcome) if annotate is not None and outcome is not None else {}
                log.add(name, _args(kwargs), outcome=outcome.outcome.value if outcome is not None else "raised",
                        stubbed=False, agent=_agent(message), **extra)
        return read
