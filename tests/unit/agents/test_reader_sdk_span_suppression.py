"""The document reader's provider calls run with the provider SDK's own span suppressed (that span would carry the
file's bytes); our `llm.call` span still exports and a failed call leaves one structured WARNING. Any other agent's
call keeps its SDK span. The SDK is the real anthropic client, instrumented by a local logfire instance, talking to a
loopback stub."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import logfire
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.agents.base_agent import BaseAgent
from src.agents.drive_document_reader_agent import AGENT_TYPE, DriveDocumentReaderAgent
from src.domain.agent import AgentConfig, AgentResponse
from src.domain.drive_extraction import DriveModelUnavailable
from src.infrastructure.drive_model_call import DriveModelCallFailed
from src.domain.llm import LLMResponse, ProviderCapabilities, UsageMetadata
from src.domain.user import PerformanceTier
from src.ports.llm_port import LLMPort, LLMRequest, Message, MessagePart
from src.utils.logfire_redacting_exporter import RedactingExporter

from tests.unit.agents.drive_model_fakes import context, prompt_builder
from tests.unit.utils.file_span_fixtures import SENTENCE, b64, make_pdf

READER_JSON = json.dumps({"text": SENTENCE, "capture": "digital", "brief": None})


class _Stub(BaseHTTPRequestHandler):
    fail = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        if _Stub.fail:
            self.send_response(400)
            body = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}).encode()
        else:
            self.send_response(200)
            body = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-stub",
                               "content": [{"type": "text", "text": READER_JSON}], "stop_reason": "end_turn",
                               "stop_sequence": None, "usage": {"input_tokens": 11, "output_tokens": 3}}).encode()
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def stub_url():
    _Stub.fail = False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def rig(monkeypatch, stub_url):
    exporter = InMemorySpanExporter()
    lf = logfire.configure(local=True, send_to_logfire=False, console=False,
                           additional_span_processors=[SimpleSpanProcessor(RedactingExporter(exporter))])
    client = anthropic.AsyncAnthropic(api_key="x", base_url=stub_url, max_retries=0)
    lf.instrument_anthropic(client, version="latest")
    tracer = lf._tracer_provider.get_tracer("llm-call-test")
    monkeypatch.setattr("src.agents.base_agent.get_tracer", lambda: tracer)
    return client, exporter


class _AnthropicLLM(LLMPort):
    """The adapter stand-in: sends the request's file part to the (instrumented) SDK client."""

    def __init__(self, client):
        self.client = client

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        blocks = []
        for part in request.messages[0].parts:
            if part.file_data:
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                              "data": part.file_data["base64"]}})
            else:
                blocks.append({"type": "text", "text": part.text})
        msg = await self.client.messages.create(model="claude-stub", max_tokens=50,
                                                messages=[{"role": "user", "content": blocks}])
        return LLMResponse(text=msg.content[0].text,
                           usage_metadata=UsageMetadata(prompt_tokens=11, completion_tokens=3, total_tokens=14))

    async def upload_file(self, path, mime_type):
        raise NotImplementedError

    def get_capabilities(self):
        return ProviderCapabilities(vision=True)

    def get_model_for_tier(self, tier: PerformanceTier):
        return "claude-stub"

    def supports_caching(self):
        return False


class _OtherAgent(BaseAgent):
    async def can_handle(self, message):
        return True

    async def execute(self, message):
        return AgentResponse.failure(task_id="t", agent_id=self.agent_id, error="unused")


def _names(exporter):
    return sorted(s.name for s in exporter.get_finished_spans())


def _reader(llm):
    return DriveDocumentReaderAgent(AgentConfig(agent_id="drive_reader_u1", agent_type=AGENT_TYPE),
                                    context(llm), prompt_builder())


async def _read(agent):
    return await agent.read(data=make_pdf(), mime_type="application/pdf", file_name="w.pdf", first_window=True,
                            account_id="acc-1")


async def test_a_reader_call_exports_our_llm_call_span_and_no_sdk_span(rig):
    client, exporter = rig
    reply = await _read(_reader(_AnthropicLLM(client)))
    assert reply.text == SENTENCE
    assert _names(exporter) == ["llm.call"]
    assert b64(make_pdf())[:40] not in json.dumps([dict(s.attributes) for s in exporter.get_finished_spans()])


async def test_another_agents_call_still_exports_its_sdk_span(rig):
    client, exporter = rig
    llm = _AnthropicLLM(client)
    agent = _OtherAgent(AgentConfig(agent_id="other_1", agent_type="other"))
    agent.llm = llm
    agent._set_execution_context(context(llm))
    request = LLMRequest(model_name="claude-stub", system_instruction="s",
                         messages=[Message(role="user", parts=[
                             MessagePart(file_data={"base64": b64(make_pdf()), "mime_type": "application/pdf",
                                                    "name": "w.pdf"}),
                             MessagePart(text=SENTENCE)])])
    await agent._call_llm(request, turn=1)
    names = _names(exporter)
    assert "llm.call" in names and len(names) == 2          # our span plus the SDK's
    sdk = next(s for s in exporter.get_finished_spans() if s.name != "llm.call")
    # the redacting exporter in front of the in-memory exporter stripped the file from the SDK span
    assert b64(make_pdf())[:40] not in sdk.attributes["gen_ai.input.messages"]
    assert "file_omitted" in sdk.attributes["gen_ai.input.messages"]
    assert SENTENCE in sdk.attributes["gen_ai.input.messages"]


async def test_a_failing_reader_call_logs_one_structured_warning_and_raises(rig, caplog):
    client, exporter = rig
    _Stub.fail = True
    with caplog.at_level(logging.WARNING):
        with pytest.raises(Exception) as err:
            await _read(_reader(_AnthropicLLM(client)))
    assert isinstance(err.value, DriveModelCallFailed | DriveModelUnavailable)   # raised on, never swallowed
    lines = [r for r in caplog.records if r.getMessage().startswith("llm_call_failed")]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    message = lines[0].getMessage()
    assert message.startswith("llm_call_failed provider=claude error_type=") and message.endswith(" window=reader")
    assert "w.pdf" not in message and SENTENCE not in message
    assert "llm.call" not in _names(exporter)                # a failed call emits no llm.call span (success path only)
    assert _names(exporter) == []                            # and no SDK span either
