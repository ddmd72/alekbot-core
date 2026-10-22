"""A scripted `LLMPort` for end-to-end tests of an agent's tool loop (TEST_COVERAGE_SYSTEMIC_RFC §5 P14, card A).

The model is the only thing scripted: the agent's own `_call_llm`, tool loop, parsing and dispatch run for real. The
shapes are promoted from `tests/unit/agents/drive_model_fakes.py` (copied, not imported: no `tests/support` module
imports from `tests/unit`), with one change that matters: **a script that runs out does not look like a refusal.**

Every agent turns an exception from its model into a failure response, so a short script would read as "the code
refused". Both fakes count the requests that found nothing to answer (`unanswered`) and raise `ScriptExhausted`, a
`RuntimeError` and deliberately not an `AssertionError` (a strict `xfail(raises=AssertionError)` would accept one). A
test ends with `assert llm.unanswered == 0 and llm.pending() == 0`; inside a strict xfail it `require`s the same.

Helpers:
- `execution_context(llm, agent_type)` puts the fake behind an `AgentExecutionContext` with a real in-memory breaker.
- `tools(("name", {args}), ...)` and `text("...")` build the model's answers.
- `tool_results(llm)` reads back what the model was told: the tool results of each request's last message, in request
  order (every agent re-sends the whole transcript, so earlier results repeat in later requests; only the last message
  is new). JSON strings are decoded; any other string (Notes' Python repr) is kept as it is.
- `first_user_text(llm)` is the text of the first user message of the first request (the agent's prompt input).
"""
from __future__ import annotations

import inspect
import json
from typing import Any, Callable, List, Optional, Sequence, Tuple, Union

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.domain.llm import LLMRequest, LLMResponse, MessagePart, ProviderCapabilities, ToolCall
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort

Scripted = Union[LLMResponse, BaseException]

MODEL = "scripted-model"


class ScriptExhausted(RuntimeError):
    """The fake model was asked for more answers than the test scripted. Never an `AssertionError`."""


class _FakeModel(LLMPort):
    """The `LLMPort` methods nobody under test should depend on."""

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        raise NotImplementedError("the scripted model takes no uploads")

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return MODEL

    def supports_caching(self) -> bool:
        return False


class ScriptedLLM(_FakeModel):
    """Answers in order; an exception item is raised. Records a deep copy of every request (`DelegationEngine` builds
    each turn's request over one shared message list, so a recorded reference would show the final transcript). An
    empty script counts the request in `unanswered` and raises `ScriptExhausted`."""

    def __init__(self, script: Sequence[Scripted]) -> None:
        self.script: List[Scripted] = list(script)
        self.requests: List[LLMRequest] = []
        self.unanswered = 0

    def pending(self) -> int:
        """Scripted answers not consumed yet."""
        return len(self.script)

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request.model_copy(deep=True))  # a snapshot: callers keep appending to one list
        if not self.script:
            self.unanswered += 1
            raise ScriptExhausted("no scripted response")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class RoutedLLM(_FakeModel):
    """Answers each request through `handler(request)` (sync or async), so the passes of one run can be told apart by
    their content. The handler returns an `LLMResponse` or an exception (raised). Returning None or raising
    `ScriptExhausted` counts the request in `unanswered` and raises `ScriptExhausted`. `pending` is the test's count
    of the per-pass queues the handler drains (0 when not given). Tracks the peak number of calls in flight."""

    def __init__(self, handler: Callable[[LLMRequest], Any], *, pending: Optional[Callable[[], int]] = None) -> None:
        self.handler = handler
        self._pending = pending
        self.requests: List[LLMRequest] = []
        self.unanswered = 0
        self.in_flight = 0
        self.peak = 0

    def pending(self) -> int:
        return self._pending() if self._pending is not None else 0

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request.model_copy(deep=True))  # a snapshot: callers keep appending to one list
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            result = self.handler(request)
            if inspect.isawaitable(result):
                result = await result
        except ScriptExhausted:
            self.unanswered += 1
            raise
        finally:
            self.in_flight -= 1
        if result is None:
            self.unanswered += 1
            raise ScriptExhausted("the handler had no response for this request")
        if isinstance(result, BaseException):
            raise result
        return result


def execution_context(llm: LLMPort, agent_type: str, model: str = MODEL,
                      provider_name: Optional[str] = None) -> AgentExecutionContext:
    """The fake model behind an execution context with a real in-memory breaker. `provider_name=None` leaves the
    field at its default (it is a plain string on the context)."""
    extra = {} if provider_name is None else {"provider_name": provider_name}
    return AgentExecutionContext(agent_type=agent_type, provider=llm, model_name=model,
                                 tier=PerformanceTier.BALANCED, capabilities=ProviderCapabilities(),
                                 resilience_port=InMemoryProviderResilience(), **extra)


def tools(*calls: Tuple[str, dict]) -> LLMResponse:
    """A model turn that calls the given tools, in order."""
    return LLMResponse(text=None, tool_calls=[ToolCall(name=name, args=args) for name, args in calls])


def text(t: str) -> LLMResponse:
    """A model turn that answers with text and calls nothing."""
    return LLMResponse(text=t, tool_calls=[])


def _decoded(result: Any) -> Any:
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    return result


def tool_results(llm: Any) -> List[Tuple[str, Any]]:
    """`(name, result)` of the tool results in each recorded request's last message, in request order. `result` is
    `response["result"]` when present, else `response`; a JSON string is decoded, any other string kept."""
    out: List[Tuple[str, Any]] = []
    for request in llm.requests:
        if not request.messages:
            continue
        for part in request.messages[-1].parts:
            if not part.tool_response:
                continue
            response = part.tool_response.get("response")
            result = response["result"] if isinstance(response, dict) and "result" in response else response
            out.append((part.tool_response.get("name"), _decoded(result)))
    return out


def first_user_text(llm: Any) -> str:
    """The text of the first user message of the first request ("" when it has none)."""
    first = next((m for m in llm.requests[0].messages if m.role == "user"), None)
    return "".join(p.text or "" for p in first.parts) if first is not None else ""


# ----------------------------------------------------------------------
# A scripted `_call_llm` for specialist tests: replies in order, remembers the requests it was given.
# ----------------------------------------------------------------------

Reply = Union[LLMResponse, Callable[[], Any]]


def script(*responses: Reply) -> Tuple[Callable[..., Any], List[LLMRequest]]:
    """`(call_llm, seen)`: the n-th call returns the n-th reply (a callable reply is awaited); `seen` collects the
    requests."""
    seen: List[LLMRequest] = []

    async def call(request: LLMRequest, turn: int = 0) -> LLMResponse:
        seen.append(request)
        reply = responses[len(seen) - 1]
        if callable(reply):
            return await reply()
        return reply
    return call, seen


def calls(*pairs: Tuple[str, dict]) -> LLMResponse:
    """One model turn that emits these `(tool name, args)` calls."""
    return LLMResponse(text="", tool_calls=[ToolCall(name=n, args=a) for n, a in pairs])
