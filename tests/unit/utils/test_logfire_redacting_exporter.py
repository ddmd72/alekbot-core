"""RedactingExporter: file parts of LLM spans never reach the inner exporter; everything else passes unchanged."""
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.utils.logfire_redacting_exporter import RedactingExporter, redact_json

from tests.unit.utils.file_span_fixtures import (
    SENTENCE, anthropic_messages, b64, gemini_messages, make_pdf, make_png, openai_messages,
)

IN, OUT, SYS = "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions"


def _export(attrs, *, drop_pending=False, names=("llm",)):
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingExporter(inner, drop_pending=drop_pending)))
    tracer = provider.get_tracer("t")
    for name in names:
        span = tracer.start_span(name)
        for k, v in attrs.items():
            span.set_attribute(k, v)
        span.end()
    return inner.get_finished_spans()


@pytest.mark.parametrize("build", [anthropic_messages, openai_messages, gemini_messages])
def test_the_file_part_is_omitted_and_the_text_part_kept_for_every_sdk_shape(build):
    data = make_pdf()
    (span,) = _export({IN: build(data), "gen_ai.request.model": "m-1", "gen_ai.usage.input_tokens": 11})
    wire = span.attributes[IN]
    assert b64(data)[:40] not in wire and b64(data) not in wire
    parts = json.loads(wire)[0]["parts"]
    assert parts[1] == {"type": "text", "content": SENTENCE}
    assert parts[0]["type"] == "file_omitted" and parts[0]["mime_type"] == "application/pdf"
    assert span.attributes["gen_ai.request.model"] == "m-1" and span.attributes["gen_ai.usage.input_tokens"] == 11


def test_an_image_is_omitted_with_its_kind_and_mime_type():
    (span,) = _export({IN: anthropic_messages(make_png(), kind="image", mime="image/png")})
    first = json.loads(span.attributes[IN])[0]["parts"][0]
    assert first == {"type": "file_omitted", "orig_type": "image", "mime_type": "image/png"}


def test_output_messages_and_system_instructions_are_redacted_too():
    data = make_pdf()
    sys_parts = json.dumps([{"type": "text", "content": "be brief"}, {"type": "blob", "mime_type": "application/pdf",
                                                                      "content": b64(data)}])
    (span,) = _export({OUT: gemini_messages(data), SYS: sys_parts})
    assert b64(data) not in span.attributes[OUT] and b64(data) not in span.attributes[SYS]
    assert json.loads(span.attributes[SYS])[0] == {"type": "text", "content": "be brief"}


def test_tool_and_reasoning_parts_are_kept():
    msgs = json.dumps([{"role": "assistant", "parts": [
        {"type": "tool_call", "id": "1", "name": "f", "arguments": {"a": 1}},
        {"type": "reasoning", "content": "because"}]}])
    (span,) = _export({OUT: msgs})
    assert span.attributes[OUT] == msgs


def test_a_value_that_is_not_json_passes_unchanged():
    assert redact_json("not json {") == "not json {"
    (span,) = _export({IN: "not json {", OUT: json.dumps({"a": 1})})
    assert span.attributes[IN] == "not json {" and span.attributes[OUT] == json.dumps({"a": 1})


def test_a_span_without_a_file_part_exports_byte_identical_attributes():
    text_only = json.dumps([{"role": "user", "parts": [{"type": "text", "content": SENTENCE}]}], indent=2)
    attrs = {IN: text_only, "llm.model": "m-1", "n": 3}
    (span,) = _export(attrs)
    assert dict(span.attributes) == attrs


def test_the_live_span_is_not_touched():
    data = make_pdf()
    seen = []

    class Spy(InMemorySpanExporter):
        def export(self, spans):
            seen.extend(spans)
            return super().export(spans)

    provider = TracerProvider()
    live = []

    class Keep(SimpleSpanProcessor):
        def on_end(self, span):
            live.append(span)
            super().on_end(span)

    provider.add_span_processor(Keep(RedactingExporter(Spy())))
    span = provider.get_tracer("t").start_span("llm")
    span.set_attribute(IN, anthropic_messages(data))
    span.end()
    assert b64(data) in live[0].attributes[IN]
    assert b64(data) not in seen[0].attributes[IN]


def test_pending_records_are_dropped_with_their_final_span_in_the_batch_and_kept_without_it():
    from opentelemetry import trace

    def span_of(tracer, name, kind, parent_ctx=None):
        ctx = trace.set_span_in_context(trace.NonRecordingSpan(parent_ctx)) if parent_ctx else None
        s = tracer.start_span(name, context=ctx)
        s.set_attribute("logfire.span_type", kind)
        s.end()
        return s

    tracer = TracerProvider().get_tracer("t")
    final = span_of(tracer, "a", "span")
    paired = span_of(tracer, "a-pending", "pending_span", final.get_span_context())
    lonely_parent = span_of(tracer, "b", "span")
    lonely = span_of(tracer, "b-pending", "pending_span", lonely_parent.get_span_context())

    def exported(drop_pending):
        inner = InMemorySpanExporter()
        RedactingExporter(inner, drop_pending=drop_pending).export([final, paired, lonely])
        return [s.name for s in inner.get_finished_spans()]

    assert exported(False) == ["a", "a-pending", "b-pending"]
    assert exported(True) == ["a", "b-pending"]


def test_shutdown_and_flush_reach_the_inner_exporter():
    inner = InMemorySpanExporter()
    exporter = RedactingExporter(inner)
    assert exporter.force_flush() is True
    exporter.shutdown()
    assert inner._stopped is True
