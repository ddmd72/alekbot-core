import os
import time
from contextvars import ContextVar
from typing import Dict, Optional, Any
from opentelemetry import trace, context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.propagate import inject, extract

_TRACE_ID_CTX: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)
_SESSION_ID_CTX: ContextVar[Optional[str]] = ContextVar("session_id", default=None)
_EVENT_ID_CTX: ContextVar[Optional[str]] = ContextVar("event_id", default=None)
_USER_ID_CTX: ContextVar[Optional[str]] = ContextVar("user_id", default=None)

_tracer = None


def init_telemetry(service_name: str = "alek-core") -> None:
    """Initialize OpenTelemetry with Cloud Trace exporter."""
    global _tracer

    resource = Resource.create({
        "service.name": service_name,
        "service.version": os.getenv("SERVICE_VERSION", "unknown")
    })

    provider = TracerProvider(resource=resource)
    
    # Only enable Cloud Trace in production to avoid local auth errors
    if os.getenv("APP_ENV", "development").lower() == "production":
        try:
            exporter = CloudTraceSpanExporter()
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
        except Exception as e:
            print(f"⚠️ Failed to initialize Cloud Trace exporter: {e}")

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)


def get_tracer():
    return _tracer or trace.get_tracer("alek-core")


def set_request_context(
    trace_id: Optional[str] = None,
    session_id: Optional[str] = None,
    event_id: Optional[str] = None,
    user_id: Optional[str] = None
) -> None:
    if trace_id:
        _TRACE_ID_CTX.set(trace_id)
    if session_id:
        _SESSION_ID_CTX.set(session_id)
    if event_id:
        _EVENT_ID_CTX.set(event_id)
    if user_id:
        _USER_ID_CTX.set(user_id)


def get_request_context() -> Dict[str, Optional[str]]:
    return {
        "trace_id": _TRACE_ID_CTX.get(),
        "session_id": _SESSION_ID_CTX.get(),
        "event_id": _EVENT_ID_CTX.get(),
        "user_id": _USER_ID_CTX.get()
    }


def _format_trace_id(trace_id_int: int) -> str:
    return f"tr_{trace_id_int:032x}"


def _format_span_id(span_id_int: int) -> str:
    return f"sp_{span_id_int:016x}"


def get_trace_ids() -> Dict[str, Optional[str]]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        return {
            "trace_id": _format_trace_id(ctx.trace_id),
            "span_id": _format_span_id(ctx.span_id)
        }
    return {"trace_id": _TRACE_ID_CTX.get(), "span_id": None}


_ALLOWED_ATTR_TYPES = (bool, str, bytes, int, float)


def _sanitize_attribute(value: Any):
    if value is None:
        return None
    if isinstance(value, _ALLOWED_ATTR_TYPES):
        return value
    if isinstance(value, (list, tuple)):
        cleaned = [item for item in (_sanitize_attribute(v) for v in value) if item is not None]
        return cleaned or None
    return str(value)


def start_span(name: str, attributes: Optional[Dict[str, Any]] = None, ctx: Optional[context.Context] = None):
    tracer = get_tracer()
    span_cm = tracer.start_as_current_span(name, context=ctx)
    if not attributes:
        return span_cm

    cleaned = {
        key: _sanitize_attribute(value)
        for key, value in attributes.items()
    }
    cleaned = {key: value for key, value in cleaned.items() if value is not None}
    if not cleaned:
        return span_cm

    class _SpanWrapper:
        def __enter__(self):
            span = span_cm.__enter__()
            for key, value in cleaned.items():
                span.set_attribute(key, value)
            return span

        def __exit__(self, exc_type, exc, tb):
            return span_cm.__exit__(exc_type, exc, tb)

    return _SpanWrapper()


def inject_trace_headers(headers: Dict[str, str]) -> None:
    inject(headers)


def extract_context(headers: Dict[str, str]) -> context.Context:
    return extract(headers)


def build_trace_id(event_id: Optional[str]) -> str:
    suffix = (event_id or str(int(time.time() * 1000)))[:12]
    return f"tr_{suffix}"
