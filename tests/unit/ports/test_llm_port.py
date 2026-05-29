import inspect
import pytest
from unittest.mock import AsyncMock

from src.ports.llm_port import (
    LLMPort,
    ProviderCapabilities,
    LLMRequest,
    LLMResponse,
    Message,
    MessagePart,
    PromptCacheConfig,
)


# ---------------------------------------------------------------------------
# LLMPort ABC contract — generate_content signature
# ---------------------------------------------------------------------------

class _ConcreteService(LLMPort):
    """Minimal concrete implementation used to verify the ABC contract."""

    async def generate_content(
        self,
        request=None,
        model_name=None,
        system_instruction=None,
        messages=None,
        **kwargs,
    ) -> LLMResponse:
        return LLMResponse(text="ok")

    def supports_caching(self) -> bool:
        return False

    async def upload_file(self, path, mime_type):
        return MessagePart(text="")

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def get_model_for_tier(self, tier) -> str:
        return "test-model"


def test_generate_content_abc_is_request_only():
    """ABC signature declares request as the sole required parameter."""
    sig = inspect.signature(LLMPort.generate_content)
    params = list(sig.parameters.keys())
    assert params == ["self", "request"]
    assert sig.parameters["request"].default is inspect.Parameter.empty


async def test_generate_content_request_path_accepted():
    """Concrete implementation can be called via request= path."""
    svc = _ConcreteService()
    req = LLMRequest(
        model_name="test-model",
        messages=[Message(role="user", parts=[MessagePart(text="hi")])],
    )
    result = await svc.generate_content(request=req)
    assert result.text == "ok"


# ---------------------------------------------------------------------------
# ProviderCapabilities and LLMRequest defaults
# ---------------------------------------------------------------------------

def test_provider_capabilities_defaults():
    caps = ProviderCapabilities()
    assert caps.native_tools is False
    assert caps.context_caching is False
    assert caps.vision is False
    assert caps.max_context_window == 32000
    assert caps.supports_system_prompt is True
    assert caps.supports_json_mode is False


def test_llm_request_defaults():
    message = Message(role="user", parts=[MessagePart(text="hi")])
    request = LLMRequest(model_name="test-model", messages=[message])
    assert request.model_name == "test-model"
    assert request.system_instruction is None
    assert request.temperature == 0.7
    assert request.max_tokens is None
    assert request.tools is None