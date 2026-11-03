"""The provider `_init_logfire` builds with content capture on: no file bytes leave the process through either the
Logfire intake or Cloud Trace; capture off keeps today's configuration."""
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src.utils.telemetry as telem
from src.utils.logfire_redacting_exporter import RedactingExporter

from tests.unit.utils.file_span_fixtures import SENTENCE, anthropic_messages, b64, make_pdf

IN = "gen_ai.input.messages"


class _RecordingOtlp(InMemorySpanExporter):
    instances = []

    def __init__(self, **kwargs):
        super().__init__()
        self.kwargs = kwargs
        _RecordingOtlp.instances.append(self)


@pytest.fixture
def wired(monkeypatch):
    """Real logfire module with `configure` recorded; OTLP and Cloud Trace exporters replaced by in-memory ones."""
    import logfire
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    _RecordingOtlp.instances = []
    cloud = InMemorySpanExporter()
    calls = []
    monkeypatch.setattr(logfire, "configure", lambda **kw: calls.append(kw))
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", _RecordingOtlp)
    monkeypatch.setattr(telem, "CloudTraceSpanExporter", lambda: cloud)
    instrumented = []
    monkeypatch.setattr(telem, "_instrument_llm_sdks", lambda lf: instrumented.append(lf))
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_v1_us_testtoken")
    monkeypatch.setenv("APP_ENV", "development")
    return calls, cloud, instrumented


def _emit_through(processors, data):
    provider = TracerProvider()
    for p in processors:
        provider.add_span_processor(p)
    span = provider.get_tracer("t").start_span("llm")
    span.set_attribute(IN, anthropic_messages(data))
    span.set_attribute("logfire.span_type", "span")
    span.end()
    provider.force_flush()


def test_capture_on_exports_no_file_bytes_through_logfire_or_cloud_trace(monkeypatch, wired):
    calls, cloud, instrumented = wired
    monkeypatch.setenv("LOGFIRE_CAPTURE_CONTENT", "true")
    data = make_pdf()

    assert telem._init_logfire("alek-core", also_cloud_trace=True) is True

    (kw,) = calls
    assert kw["send_to_logfire"] is False and kw["console"] is False and kw["token"] == "pylf_v1_us_testtoken"
    assert len(kw["additional_span_processors"]) == 2 and instrumented
    _emit_through(kw["additional_span_processors"], data)
    (intake,) = _RecordingOtlp.instances
    for exported in (intake.get_finished_spans(), cloud.get_finished_spans()):
        (span,) = exported
        assert b64(data)[:40] not in span.attributes[IN]
        assert json.loads(span.attributes[IN])[0]["parts"][1] == {"type": "text", "content": SENTENCE}
    assert intake.kwargs["endpoint"] == "https://logfire-us.pydantic.dev/v1/traces"
    assert intake.kwargs["headers"]["Authorization"] == "pylf_v1_us_testtoken"


def test_capture_on_without_cloud_trace_has_only_the_logfire_intake(monkeypatch, wired):
    calls, cloud, _ = wired
    monkeypatch.setenv("LOGFIRE_CAPTURE_CONTENT", "true")
    telem._init_logfire("alek-core", also_cloud_trace=False)
    assert len(calls[0]["additional_span_processors"]) == 1


def test_capture_off_keeps_todays_configuration(monkeypatch, wired):
    calls, cloud, instrumented = wired
    monkeypatch.delenv("LOGFIRE_CAPTURE_CONTENT", raising=False)
    sentinel = object()
    monkeypatch.setattr(telem, "_make_cloud_trace_processor", lambda: sentinel)

    assert telem._init_logfire("alek-core", also_cloud_trace=True) is True

    (kw,) = calls
    assert kw == {"token": "pylf_v1_us_testtoken", "service_name": "alek-core", "service_version": "unknown",
                  "environment": "development", "console": False, "send_to_logfire": True,
                  "additional_span_processors": [sentinel]}
    assert instrumented == [] and _RecordingOtlp.instances == []


def test_cloud_trace_exporter_is_unwrapped_when_capture_is_off(monkeypatch, wired):
    _, cloud, _ = wired
    monkeypatch.delenv("LOGFIRE_CAPTURE_CONTENT", raising=False)
    processor = telem._make_cloud_trace_processor()
    assert processor.span_exporter is cloud
    wrapped = telem._make_cloud_trace_processor(redact_files=True)
    assert isinstance(wrapped.span_exporter, RedactingExporter)


def test_when_the_redacting_exporter_cannot_be_built_content_capture_stays_off(monkeypatch, wired, capsys):
    calls, _, instrumented = wired
    monkeypatch.setenv("LOGFIRE_CAPTURE_CONTENT", "true")

    def broken(token):
        raise RuntimeError("no exporter")
    monkeypatch.setattr(telem, "_logfire_redacting_processor", broken)

    assert telem._init_logfire("alek-core", also_cloud_trace=False) is True

    assert instrumented == []                      # fail closed: no instrumentation, so no file bytes anywhere
    assert calls[0]["send_to_logfire"] is True
    assert "content capture off" in capsys.readouterr().out


def test_sdk_spans_suppressed_is_a_no_op_without_logfire(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_logfire(name, *a, **k):
        if name == "logfire":
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_logfire)
    with telem.sdk_spans_suppressed():
        pass
