"""`LLMPort.native_formats` (EXTERNAL_FILES_RFC §4.3, P5): concrete, text only by default, and forwarded unchanged by
both proxies an agent's provider is wrapped in."""
from src.domain.native_format import NativeFormat
from src.domain.user import PerformanceTier
from src.infrastructure.agent_manifest import NATIVE_OPENING_INTENTS, Intent
from src.ports.llm_port import LLMPort, LLMRequest, LLMResponse, MessagePart, PromptCacheConfig, ProviderCapabilities
from src.services.alerting_llm_proxy import AlertingLLMProxy
from src.services.caching_llm_proxy import CachingLLMProxy

PDF = NativeFormat(mime="application/pdf", max_bytes=20 * 1024 * 1024, max_pages=100)
PNG = NativeFormat(mime="image/png", max_bytes=5 * 1024 * 1024, max_pages=1)


class _TextOnly(LLMPort):
    """An adapter that does not override `native_formats`, like the existing test fakes."""

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="ok")

    def supports_caching(self) -> bool:
        return False

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        return MessagePart(text="")

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return "model-a"


class _Declaring(_TextOnly):
    """An adapter whose declaration depends on the model: only `model-pdf` reads PDFs."""

    def native_formats(self, model_name: str) -> tuple[NativeFormat, ...]:
        return (PNG, PDF) if model_name == "model-pdf" else (PNG,)


async def _no_alert(text: str) -> None:
    return None


def test_the_method_is_concrete_so_existing_fakes_need_no_edit():
    assert "native_formats" not in LLMPort.__abstractmethods__


def test_the_default_declaration_is_text_only():
    assert _TextOnly().native_formats("any-model") == ()


def test_the_caching_proxy_forwards_the_model_name_and_the_declaration():
    proxy = CachingLLMProxy(_Declaring(), PromptCacheConfig(enabled=True))
    assert proxy.native_formats("model-pdf") == (PNG, PDF)
    assert proxy.native_formats("model-img") == (PNG,)


def test_the_alerting_proxy_forwards_the_model_name_and_the_declaration():
    proxy = AlertingLLMProxy(_Declaring(), alert_fn=_no_alert)
    assert proxy.native_formats("model-pdf") == (PNG, PDF)
    assert proxy.native_formats("model-img") == (PNG,)


def test_the_production_stack_forwards_through_both_proxies():
    stacked = AlertingLLMProxy(CachingLLMProxy(_Declaring(), PromptCacheConfig(enabled=True)), alert_fn=_no_alert)
    assert stacked.native_formats("model-pdf") == (PNG, PDF)
    assert AlertingLLMProxy(CachingLLMProxy(_TextOnly(), PromptCacheConfig()), alert_fn=_no_alert).native_formats(
        "model-pdf") == ()


def test_the_opening_intents_are_named_in_the_manifest():
    assert NATIVE_OPENING_INTENTS == frozenset({Intent.OPEN_FILE, Intent.WORK_WITH_DRIVE_FILES})
