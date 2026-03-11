"""Transparent LLM proxy that injects prompt cache configuration.

Agents receive this proxy instead of the raw LLM provider.
They call generate_content() as usual — the proxy enriches
the request with cache_config before forwarding to the real adapter.

See: docs/10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md
"""

from typing import List, Any, Optional

from ..ports.llm_port import (
    LLMPort,
    LLMRequest,
    LLMResponse,
    PromptCacheConfig,
    ProviderCapabilities,
    AutomaticFunctionCallingConfig,
    Message,
    MessagePart,
)
from ..domain.user import PerformanceTier


class CachingLLMProxy(LLMPort):
    """Transparent proxy that injects prompt cache config into LLM requests.

    Agents receive this proxy instead of the raw provider via AgentExecutionContext.
    The proxy enriches each LLMRequest with the resolved cache_config,
    then forwards to the real adapter. If the request already has an explicit
    cache_config, the proxy respects it and does not override.
    """

    def __init__(self, inner: LLMPort, cache_config: PromptCacheConfig):
        self._inner = inner
        self._cache_config = cache_config

    async def generate_content(
        self,
        request: Optional[LLMRequest] = None,
        model_name: Optional[str] = None,
        system_instruction: Optional[str] = None,
        messages: Optional[List[Message]] = None,
        tools: Optional[List[Any]] = None,
        temperature: float = 0.7,
        stream_callback: Optional[Any] = None,
        response_mime_type: Optional[str] = None,
        response_schema: Optional[Any] = None,
        cache_config: Optional[PromptCacheConfig] = None,
        automatic_function_calling: Optional[AutomaticFunctionCallingConfig] = None,
    ) -> LLMResponse:
        # LLMRequest path (all current agents use this)
        if request is not None:
            if not request.cache_config and self._cache_config:
                request = request.model_copy(
                    update={"cache_config": self._cache_config}
                )
            return await self._inner.generate_content(request=request)

        # Legacy parameter path (defensive coverage)
        effective_cache = cache_config if cache_config else self._cache_config
        return await self._inner.generate_content(
            model_name=model_name,
            system_instruction=system_instruction,
            messages=messages,
            tools=tools,
            temperature=temperature,
            stream_callback=stream_callback,
            response_mime_type=response_mime_type,
            response_schema=response_schema,
            cache_config=effective_cache,
            automatic_function_calling=automatic_function_calling,
        )

    def supports_caching(self) -> bool:
        return self._inner.supports_caching()

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        return await self._inner.upload_file(path, mime_type)

    def get_capabilities(self) -> ProviderCapabilities:
        return self._inner.get_capabilities()

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return self._inner.get_model_for_tier(tier)
