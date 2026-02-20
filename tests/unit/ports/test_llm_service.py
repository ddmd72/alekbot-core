from src.ports.llm_service import ProviderCapabilities, LLMRequest, Message, MessagePart


def test_provider_capabilities_defaults():
    caps = ProviderCapabilities()
    assert caps.native_tools is False
    assert caps.streaming is True
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
    assert request.stream is False