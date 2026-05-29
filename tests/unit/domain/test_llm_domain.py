"""
Unit tests for LLM domain models — verifies Pydantic validation, defaults, and re-export paths.

Covers:
- LLMRequest, LLMResponse, ProviderCapabilities, UsageMetadata field defaults
- AgentExecutionContext validates correctly in ports/ (requires LLMPort provider)
- Backward-compatible import from src.ports.llm_port still works
"""
import pytest

from src.domain.llm import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
    UsageMetadata,
    PromptCacheConfig,
    CacheMetadata,
    AutomaticFunctionCallingConfig,
    Message,
    MessagePart,
    ToolCall,
    PROMPT_CACHE_BOUNDARY,
)


class TestLLMRequest:
    def _make_message(self):
        return Message(role="user", parts=[MessagePart(text="hi")])

    def test_required_fields(self):
        req = LLMRequest(model_name="test-model", messages=[self._make_message()])
        assert req.model_name == "test-model"
        assert req.temperature == 0.7
        assert req.stream is False

    def test_defaults(self):
        req = LLMRequest(model_name="m", messages=[self._make_message()])
        assert req.tools is None
        assert req.system_instruction is None
        assert req.force_tool_use is False
        assert req.use_grounding is False
        assert req.thinking is None

    def test_missing_model_name_raises(self):
        with pytest.raises(Exception):
            LLMRequest(messages=[self._make_message()])

    def test_unknown_kwarg_rejected(self):
        """R14.3 guard: LLMRequest must reject unknown kwargs at construction.

        Without ConfigDict(extra='forbid'), Pydantic V2 silently dropped extras —
        a 2026-03-16 commit renamed max_tokens to max_output_tokens in
        DocGeneratorAgent and the regression went undetected for ~46 days.
        """
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            LLMRequest(
                model_name="m",
                messages=[self._make_message()],
                max_output_tokens=64_000,  # not a valid LLMRequest field
            )

    def test_max_tokens_accepted(self):
        """The canonical max_tokens field must remain accepted."""
        req = LLMRequest(
            model_name="m",
            messages=[self._make_message()],
            max_tokens=64_000,
        )
        assert req.max_tokens == 64_000

    def test_typo_unknown_kwarg_rejected(self):
        """Generic typo (max_token, not max_tokens) must also be rejected — proves
        the guard is broad, not a special-cased max_output_tokens block."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            LLMRequest(
                model_name="m",
                messages=[self._make_message()],
                max_token=64_000,
            )


class TestLLMResponse:
    def test_all_defaults_none(self):
        resp = LLMResponse()
        assert resp.text is None
        assert resp.tool_calls == []
        assert resp.usage_metadata is None
        assert resp.cache_metadata is None

    def test_with_text(self):
        resp = LLMResponse(text="hello")
        assert resp.text == "hello"


class TestProviderCapabilities:
    def test_conservative_defaults(self):
        caps = ProviderCapabilities()
        assert caps.native_tools is False
        assert caps.context_caching is False
        assert caps.native_grounding is False
        assert caps.supports_reasoning is False
        assert caps.max_context_window == 32000

    def test_custom_capabilities(self):
        caps = ProviderCapabilities(native_tools=True, context_caching=True, max_context_window=128000)
        assert caps.native_tools is True
        assert caps.max_context_window == 128000


class TestUsageMetadata:
    def test_zero_defaults(self):
        u = UsageMetadata()
        assert u.prompt_tokens == 0
        assert u.completion_tokens == 0
        assert u.total_tokens == 0

    def test_custom_values(self):
        u = UsageMetadata(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        assert u.total_tokens == 150


class TestPromptCacheConfig:
    def test_disabled_by_default(self):
        cfg = PromptCacheConfig()
        assert cfg.enabled is False


class TestPromptCacheBoundary:
    def test_constant_value(self):
        assert PROMPT_CACHE_BOUNDARY == "<!-- CACHE_BOUNDARY -->"


class TestBackwardCompatImport:
    """Verify that importing from ports.llm_port still works (re-export path)."""

    def test_import_from_llm_port(self):
        from src.ports.llm_port import (
            LLMRequest as LLMRequestFromPort,
            LLMResponse as LLMResponseFromPort,
            ProviderCapabilities as CapFromPort,
            PROMPT_CACHE_BOUNDARY as BoundaryFromPort,
        )
        # Same objects — re-exported from domain
        assert LLMRequestFromPort is LLMRequest
        assert LLMResponseFromPort is LLMResponse
        assert CapFromPort is ProviderCapabilities
        assert BoundaryFromPort == PROMPT_CACHE_BOUNDARY
