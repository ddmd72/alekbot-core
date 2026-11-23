"""TaintTracker — one execution attempt's answer to "did outside content enter it?" (EXTERNAL_TASK_QUEUE_RFC §5.3).

Built per execution attempt by `SmartResponseAgent` and shared by two readers of one fact: the `DelegationEngine`
feeds it every tool batch (`observe_batch`, passed as the engine's `observe`), and the `create_external_task` handler
reads `tainted` to decide between queueing a task and holding it. The rule itself is `domain/execution_taint.py`.

The flag only ever turns on. `observe_batch` is called after a batch has run and before the model sees its results,
so a create issued in a later turn sees every earlier result; a create in the same batch as a read was written before
the model read anything of it.

Public interface:
    TaintTracker()
    .tainted -> bool
    .mark() -> None                                              taint the execution (a read of a worker's text)
    .observe_request(messages, static_blocks=()) -> None         once, before the first model call
    .observe_batch(calls, results) -> None                       after each tool batch
"""
from typing import Iterable, Sequence

from ..domain.execution_taint import request_taints, result_taints
from ..domain.llm import Message, ToolCall
from .delegation_engine import ToolResult


class TaintTracker:
    def __init__(self) -> None:
        self._tainted = False

    @property
    def tainted(self) -> bool:
        return self._tainted

    def mark(self) -> None:
        self._tainted = True

    def observe_request(self, messages: Iterable[Message], static_blocks: Iterable[str] = ()) -> None:
        if request_taints(messages, static_blocks):
            self._tainted = True

    def observe_batch(self, calls: Sequence[ToolCall], results: Sequence[ToolResult]) -> None:
        """A batch whose calls and results do not pair up is tainted: a result nobody can attribute is not trusted."""
        if len(calls) != len(results):
            self._tainted = True
            return
        for call, result in zip(calls, results):
            if result.untrusted or result_taints(call.name, call.args, result.result_str, bool(result.file_data)):
                self._tainted = True
                return
