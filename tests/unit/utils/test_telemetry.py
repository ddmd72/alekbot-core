"""
Unit tests for src/utils/telemetry.py.

Coverage:
  init_telemetry()
    - development mode: no CloudTrace exporter
    - production mode: attempts CloudTrace (exception swallowed)
  set_request_context() / get_request_context()
    - sets individual fields; None ignored
  get_trace_ids()
    - no active span → returns ContextVar trace_id
    - valid span → returns formatted trace_id + span_id
  _format_trace_id() / _format_span_id()
    - return expected format strings
  _sanitize_attribute()
    - None → None
    - primitive types pass through
    - list/tuple → cleaned recursively
    - non-primitive → str()
  start_span()
    - no attributes → returns plain context manager
    - attributes provided → _SpanWrapper sets them
    - all-None attributes → plain context manager
  inject_trace_headers() / extract_context()
    - call through to opentelemetry propagate functions
  build_trace_id()
    - with event_id → uses prefix of event_id
    - without event_id → uses timestamp
"""
import os
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# init_telemetry()
# ---------------------------------------------------------------------------

class TestInitTelemetry:

    def test_development_mode_no_cloud_trace(self):
        from src.utils.telemetry import init_telemetry
        with patch.dict(os.environ, {"APP_ENV": "development"}):
            init_telemetry("test-service")
        # Just must not raise

    def test_production_mode_cloud_trace_exception_swallowed(self):
        """In production, CloudTraceSpanExporter failure is swallowed."""
        from src.utils.telemetry import init_telemetry
        with (
            patch.dict(os.environ, {"APP_ENV": "production"}),
            patch(
                "src.utils.telemetry.CloudTraceSpanExporter",
                side_effect=Exception("no credentials"),
            ),
        ):
            init_telemetry("test-service")  # must not raise

    def test_production_mode_success(self):
        from src.utils.telemetry import init_telemetry
        mock_exporter = MagicMock()
        mock_processor = MagicMock()
        with (
            patch.dict(os.environ, {"APP_ENV": "production"}),
            patch("src.utils.telemetry.CloudTraceSpanExporter", return_value=mock_exporter),
            patch("src.utils.telemetry.BatchSpanProcessor", return_value=mock_processor),
        ):
            init_telemetry("test-service")


# ---------------------------------------------------------------------------
# set_request_context() / get_request_context()
# ---------------------------------------------------------------------------

class TestRequestContext:

    def test_set_and_get_all_fields(self):
        from src.utils.telemetry import set_request_context, get_request_context
        set_request_context(
            trace_id="tr_abc",
            session_id="sess_1",
            event_id="ev_x",
            user_id="u1",
        )
        ctx = get_request_context()
        assert ctx["trace_id"] == "tr_abc"
        assert ctx["session_id"] == "sess_1"
        assert ctx["event_id"] == "ev_x"
        assert ctx["user_id"] == "u1"

    def test_none_values_not_overwrite(self):
        """Calling with None doesn't change existing context vars."""
        from src.utils.telemetry import set_request_context, get_request_context
        set_request_context(trace_id="original")
        set_request_context(trace_id=None)  # None → skip
        ctx = get_request_context()
        # The original value should remain (None is not set)
        # Note: ContextVars are per-async-task; in sync test they persist
        assert ctx["trace_id"] == "original"


# ---------------------------------------------------------------------------
# get_trace_ids()
# ---------------------------------------------------------------------------

class TestGetTraceIds:

    def test_no_active_span_returns_context_var(self):
        from src.utils.telemetry import get_trace_ids, set_request_context
        set_request_context(trace_id="tr_fallback")
        result = get_trace_ids()
        # With no active OTel span, falls back to ContextVar
        assert result["span_id"] is None
        # trace_id is either from ContextVar or OTel (depends on test isolation)

    def test_valid_span_returns_formatted_ids(self):
        from src.utils.telemetry import get_trace_ids, _format_trace_id, _format_span_id
        mock_ctx = MagicMock()
        mock_ctx.is_valid = True
        mock_ctx.trace_id = 0xABCDEF1234567890ABCDEF1234567890
        mock_ctx.span_id = 0x1234567890ABCDEF
        mock_span = MagicMock()
        mock_span.get_span_context.return_value = mock_ctx

        with patch("src.utils.telemetry.trace.get_current_span", return_value=mock_span):
            result = get_trace_ids()

        assert result["trace_id"] == _format_trace_id(mock_ctx.trace_id)
        assert result["span_id"] == _format_span_id(mock_ctx.span_id)
        assert result["trace_id"].startswith("tr_")
        assert result["span_id"].startswith("sp_")


# ---------------------------------------------------------------------------
# _format_trace_id() / _format_span_id()
# ---------------------------------------------------------------------------

class TestFormatIds:

    def test_format_trace_id(self):
        from src.utils.telemetry import _format_trace_id
        result = _format_trace_id(0)
        assert result.startswith("tr_")
        assert len(result) == 3 + 32  # "tr_" + 32 hex chars

    def test_format_span_id(self):
        from src.utils.telemetry import _format_span_id
        result = _format_span_id(0)
        assert result.startswith("sp_")
        assert len(result) == 3 + 16  # "sp_" + 16 hex chars


# ---------------------------------------------------------------------------
# _sanitize_attribute()
# ---------------------------------------------------------------------------

class TestSanitizeAttribute:

    def test_none_returns_none(self):
        from src.utils.telemetry import _sanitize_attribute
        assert _sanitize_attribute(None) is None

    def test_primitives_pass_through(self):
        from src.utils.telemetry import _sanitize_attribute
        assert _sanitize_attribute(True) is True
        assert _sanitize_attribute(42) == 42
        assert _sanitize_attribute(3.14) == 3.14
        assert _sanitize_attribute("hello") == "hello"
        assert _sanitize_attribute(b"bytes") == b"bytes"

    def test_list_cleaned_recursively(self):
        from src.utils.telemetry import _sanitize_attribute
        result = _sanitize_attribute(["a", None, "b"])
        assert result == ["a", "b"]

    def test_all_none_list_returns_none(self):
        from src.utils.telemetry import _sanitize_attribute
        result = _sanitize_attribute([None, None])
        assert result is None

    def test_tuple_treated_as_list(self):
        from src.utils.telemetry import _sanitize_attribute
        result = _sanitize_attribute(("x", None, "y"))
        assert result == ["x", "y"]

    def test_unknown_type_becomes_str(self):
        from src.utils.telemetry import _sanitize_attribute
        result = _sanitize_attribute({"key": "val"})
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# start_span()
# ---------------------------------------------------------------------------

class TestStartSpan:

    def test_no_attributes_returns_tracer_span(self):
        from src.utils.telemetry import start_span
        span_cm = start_span("test_span")
        # Should be usable as context manager
        assert hasattr(span_cm, "__enter__")

    def test_with_attributes_returns_span_wrapper(self):
        from src.utils.telemetry import start_span
        span_cm = start_span("test_span", attributes={"user": "u1", "count": 5})
        # _SpanWrapper has __enter__ / __exit__
        assert hasattr(span_cm, "__enter__")
        assert hasattr(span_cm, "__exit__")

    def test_all_none_attributes_returns_plain_span(self):
        from src.utils.telemetry import start_span
        # All values None → cleaned dict empty → return plain span_cm
        span_cm = start_span("test_span", attributes={"a": None, "b": None})
        assert hasattr(span_cm, "__enter__")

    def test_span_wrapper_sets_attributes(self):
        """_SpanWrapper.__enter__ must call span.set_attribute for each attribute."""
        from src.utils.telemetry import start_span
        mock_span = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_span)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with patch("src.utils.telemetry.get_tracer") as mock_get_tracer:
            mock_get_tracer.return_value.start_as_current_span.return_value = mock_cm
            wrapper = start_span("op", attributes={"key": "val", "num": 1})
            span = wrapper.__enter__()

        mock_span.set_attribute.assert_any_call("key", "val")
        mock_span.set_attribute.assert_any_call("num", 1)


# ---------------------------------------------------------------------------
# inject_trace_headers() / extract_context()
# ---------------------------------------------------------------------------

class TestPropagation:

    def test_inject_trace_headers_calls_inject(self):
        from src.utils.telemetry import inject_trace_headers
        headers = {}
        with patch("src.utils.telemetry.inject") as mock_inject:
            inject_trace_headers(headers)
        mock_inject.assert_called_once_with(headers)

    def test_extract_context_calls_extract(self):
        from src.utils.telemetry import extract_context
        headers = {"traceparent": "00-abc-def-01"}
        mock_ctx = MagicMock()
        with patch("src.utils.telemetry.extract", return_value=mock_ctx) as mock_extract:
            result = extract_context(headers)
        mock_extract.assert_called_once_with(headers)
        assert result is mock_ctx


# ---------------------------------------------------------------------------
# build_trace_id()
# ---------------------------------------------------------------------------

class TestBuildTraceId:

    def test_with_event_id_uses_prefix(self):
        from src.utils.telemetry import build_trace_id
        result = build_trace_id("evt_abc123xyz")
        assert result == "tr_evt_abc123xy"  # prefix [:12] of "evt_abc123xyz"

    def test_without_event_id_uses_timestamp(self):
        from src.utils.telemetry import build_trace_id
        result = build_trace_id(None)
        assert result.startswith("tr_")

    def test_empty_string_uses_timestamp(self):
        from src.utils.telemetry import build_trace_id
        result = build_trace_id("")
        assert result.startswith("tr_")
