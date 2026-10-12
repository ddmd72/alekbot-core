"""Shared fakes for the drive indexer's model agents and runners (USER_DRIVE_INDEXING_RFC §24.2): LLMPorts that answer
from a script or by request content, an execution context over them, and a prompt builder serving a complete or
broken prompt."""
from __future__ import annotations

import base64
import inspect
import json
from typing import Any, List, Optional, Sequence, Union
from unittest.mock import AsyncMock, MagicMock

from src.domain.llm import FinishReason, LLMResponse, ProviderCapabilities, UsageMetadata
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, LLMRequest
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.provider_resilience_port import ProviderResiliencePort

PROMPT = ("class DriveAgent extends Agent {\n\n    cognitive_process {\n\n    rules\n\n    }\n\n"
          "    output_format {\n\n    json\n\n    }\n\n}")

Scripted = Union[LLMResponse, BaseException]


def reply(payload: Any = None, *, text: Optional[str] = None, finish: FinishReason = FinishReason.STOP,
          prompt_tokens: int = 1000, completion_tokens: int = 200) -> LLMResponse:
    """A provider answer: `payload` as JSON text (or raw `text`), with usage for billing."""
    return LLMResponse(text=json.dumps(payload) if text is None else text, finish_reason=finish,
                       usage_metadata=UsageMetadata(prompt_tokens=prompt_tokens,
                                                    completion_tokens=completion_tokens,
                                                    total_tokens=prompt_tokens + completion_tokens))


class ScriptedLLM(LLMPort):
    """Answers in order; an exception in the script is raised. Records every request."""

    def __init__(self, script: Sequence[Scripted]) -> None:
        self.script: List[Scripted] = list(script)
        self.requests: List[LLMRequest] = []

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def upload_file(self, path: str, mime_type: str) -> Any:
        raise NotImplementedError

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(vision=True)

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return "claude-haiku-5-5"

    def supports_caching(self) -> bool:
        return False


def context(llm: LLMPort, model: str = "claude-haiku-5-5") -> AgentExecutionContext:
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.return_value = False
    return AgentExecutionContext(agent_type="drive_reader", provider=llm, model_name=model,
                                 tier=PerformanceTier.ECO, capabilities=ProviderCapabilities(vision=True),
                                 provider_name="claude", resilience_port=resilience)


def prompt_builder(prompt: Optional[str] = PROMPT, error: Optional[Exception] = None) -> AsyncMock:
    builder = AsyncMock(spec=PromptBuilderPort)
    if error is not None:
        builder.build_for_agent.side_effect = error
    else:
        builder.build_for_agent.return_value = prompt
    return builder


class RoutedLLM(LLMPort):
    """Answers each request through `handler(request)` (sync or async), so concurrent calls can be told apart by
    their content. Tracks the peak number of calls in flight."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.requests: List[LLMRequest] = []
        self.in_flight = 0
        self.peak = 0

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            result = self.handler(request)
            if inspect.isawaitable(result):
                result = await result
        finally:
            self.in_flight -= 1
        if isinstance(result, BaseException):
            raise result
        return result

    async def upload_file(self, path: str, mime_type: str) -> Any:
        raise NotImplementedError

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(vision=True)

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return "claude-haiku-5-5"

    def supports_caching(self) -> bool:
        return False


def file_bytes(request: LLMRequest) -> bytes:
    """The file the request carries (the first file part of its first message)."""
    part = next(p for p in request.messages[0].parts if p.file_data)
    return base64.b64decode(part.file_data["base64"])
