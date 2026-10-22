"""A scripted `_call_llm` for specialist tests: replies in order, remembers the requests it was given."""
from typing import Any, Callable, List, Tuple, Union

from src.domain.llm import LLMRequest, LLMResponse, ToolCall

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
