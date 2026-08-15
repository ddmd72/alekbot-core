"""
Unit tests for tracing backend selection in telemetry.init.

Covers _resolve_backend (legacy default preserved, explicit override),
_init_logfire (token gate, Logfire configured as global provider, Cloud Trace
attached as an additional span processor for the ``both`` mode) and the
LLM prompt-content capture (LOGFIRE_CAPTURE_CONTENT kill switch, GenAI semconv
instrumentation of the provider SDKs, per-SDK failure isolation).
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

import src.utils.telemetry as telem


class TestResolveBackend:
    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("TRACING_BACKEND", "Both")
        assert telem._resolve_backend() == "both"

    def test_production_default_is_cloud_trace(self, monkeypatch):
        monkeypatch.delenv("TRACING_BACKEND", raising=False)
        monkeypatch.setenv("APP_ENV", "production")
        assert telem._resolve_backend() == "cloud_trace"

    def test_development_default_is_none(self, monkeypatch):
        monkeypatch.delenv("TRACING_BACKEND", raising=False)
        monkeypatch.setenv("APP_ENV", "development")
        assert telem._resolve_backend() == "none"


class TestInitLogfire:
    @pytest.fixture
    def fake_logfire(self, monkeypatch):
        mod = MagicMock()
        mod.configure = MagicMock()
        monkeypatch.setitem(sys.modules, "logfire", mod)
        return mod

    def test_no_token_skips_and_returns_false(self, monkeypatch, fake_logfire):
        monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)

        result = telem._init_logfire("alek-core", also_cloud_trace=True)

        assert result is False
        fake_logfire.configure.assert_not_called()

    def test_configures_logfire_as_provider(self, monkeypatch, fake_logfire):
        monkeypatch.setenv("LOGFIRE_TOKEN", "tok-123")
        monkeypatch.setenv("APP_ENV", "development")

        result = telem._init_logfire("alek-core", also_cloud_trace=False)

        assert result is True
        fake_logfire.configure.assert_called_once()
        kwargs = fake_logfire.configure.call_args.kwargs
        assert kwargs["token"] == "tok-123"
        assert kwargs["service_name"] == "alek-core"
        assert kwargs["console"] is False
        assert kwargs["send_to_logfire"] is True
        # No cloud-trace fan-out requested → no extra processors.
        assert kwargs["additional_span_processors"] is None

    def test_both_attaches_cloud_trace_processor(self, monkeypatch, fake_logfire):
        monkeypatch.setenv("LOGFIRE_TOKEN", "tok-123")
        sentinel = object()
        monkeypatch.setattr(telem, "_make_cloud_trace_processor", lambda: sentinel)

        result = telem._init_logfire("alek-core", also_cloud_trace=True)

        assert result is True
        processors = fake_logfire.configure.call_args.kwargs["additional_span_processors"]
        assert processors == [sentinel]

    def test_both_skips_cloud_trace_when_processor_unavailable(self, monkeypatch, fake_logfire):
        monkeypatch.setenv("LOGFIRE_TOKEN", "tok-123")
        monkeypatch.setattr(telem, "_make_cloud_trace_processor", lambda: None)

        telem._init_logfire("alek-core", also_cloud_trace=True)

        # Cloud Trace failed to build → Logfire still configured, no extra processors.
        assert fake_logfire.configure.call_args.kwargs["additional_span_processors"] is None


class TestContentCaptureFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("LOGFIRE_CAPTURE_CONTENT", raising=False)
        assert telem._content_capture_enabled() is False

    def test_enabled_case_insensitively(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_CAPTURE_CONTENT", "True")
        assert telem._content_capture_enabled() is True

    def test_any_other_value_is_off(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_CAPTURE_CONTENT", "1")
        assert telem._content_capture_enabled() is False


class TestInstrumentLlmSdks:
    @pytest.fixture
    def fake_logfire(self, monkeypatch):
        mod = MagicMock()
        monkeypatch.setitem(sys.modules, "logfire", mod)
        return mod

    @pytest.fixture
    def fake_instrumentor(self, monkeypatch):
        """Stand in for GoogleGenAiSdkInstrumentor, imported inside the function."""
        instance = MagicMock()
        module = MagicMock()
        module.GoogleGenAiSdkInstrumentor.return_value = instance
        monkeypatch.setitem(
            sys.modules, "opentelemetry.instrumentation.google_genai", module
        )
        return instance

    def test_instruments_all_three_sdks_with_semconv(self, monkeypatch, fake_logfire, fake_instrumentor):
        monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)

        telem._instrument_llm_sdks(fake_logfire)

        # version='latest' is what emits gen_ai.* content attributes — the panels
        # and conversation view read those, not our custom llm.* ones.
        fake_logfire.instrument_anthropic.assert_called_once_with(version="latest")
        fake_logfire.instrument_openai.assert_called_once_with(version="latest")
        # Gemini deliberately does NOT go through logfire.instrument_google_genai():
        # that shim asserts the old per-message event shape and fails on every call
        # against opentelemetry-util-genai 1.0b0's consolidated event.
        fake_logfire.instrument_google_genai.assert_not_called()
        assert fake_instrumentor.instrument.call_count == 1

    def test_opts_google_genai_into_content_capture(self, monkeypatch, fake_logfire):
        monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)

        telem._instrument_llm_sdks(fake_logfire)

        # NOT a boolean: valid values are NO_CONTENT / SPAN_ONLY / EVENT_ONLY /
        # SPAN_AND_EVENT. "true" (asserted here until 2026-08-15) is rejected by the
        # instrumentation with a warning and falls back to NO_CONTENT — which is why
        # Gemini content was empty even once the packages could co-install.
        assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "SPAN_ONLY"

    def test_respects_explicit_genai_opt_out(self, monkeypatch, fake_logfire):
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false")

        telem._instrument_llm_sdks(fake_logfire)

        assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "false"

    def test_one_sdk_failure_does_not_block_the_others(self, monkeypatch, fake_logfire, fake_instrumentor):
        fake_logfire.instrument_anthropic.side_effect = RuntimeError("anthropic sdk absent")

        telem._instrument_llm_sdks(fake_logfire)

        # Tracing is diagnostic, never load-bearing: the rest still gets wired.
        fake_logfire.instrument_openai.assert_called_once_with(version="latest")
        assert fake_instrumentor.instrument.call_count == 1


class TestInitLogfireContentGate:
    @pytest.fixture
    def fake_logfire(self, monkeypatch):
        mod = MagicMock()
        mod.configure = MagicMock()
        monkeypatch.setitem(sys.modules, "logfire", mod)
        monkeypatch.setenv("LOGFIRE_TOKEN", "tok-123")
        return mod

    def test_flag_off_leaves_sdks_uninstrumented(self, monkeypatch, fake_logfire):
        monkeypatch.setenv("LOGFIRE_CAPTURE_CONTENT", "false")

        telem._init_logfire("alek-core", also_cloud_trace=False)

        fake_logfire.configure.assert_called_once()
        fake_logfire.instrument_anthropic.assert_not_called()
        fake_logfire.instrument_openai.assert_not_called()
        fake_logfire.instrument_google_genai.assert_not_called()

    def test_flag_on_instruments_after_configure(self, monkeypatch, fake_logfire):
        monkeypatch.setenv("LOGFIRE_CAPTURE_CONTENT", "true")
        calls = []
        fake_logfire.configure.side_effect = lambda **kw: calls.append("configure")
        fake_logfire.instrument_anthropic.side_effect = lambda **kw: calls.append("anthropic")

        result = telem._init_logfire("alek-core", also_cloud_trace=False)

        assert result is True
        # Instrumentation must follow configure() — it patches onto the provider
        # Logfire installs.
        assert calls == ["configure", "anthropic"]

    def test_no_token_never_instruments(self, monkeypatch, fake_logfire):
        monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
        monkeypatch.setenv("LOGFIRE_CAPTURE_CONTENT", "true")

        result = telem._init_logfire("alek-core", also_cloud_trace=False)

        assert result is False
        fake_logfire.instrument_anthropic.assert_not_called()


class TestGoogleGenAiContentMode:
    """The content mode is an enum the instrumentation validates, not a flag.

    This is what actually kept Gemini content out of Logfire: the pin conflict was
    the documented reason, but even after it cleared, `"true"` was rejected and the
    instrumentation fell back to NO_CONTENT. A wrong-but-plausible value is worse
    than a missing one — it looks configured.
    """

    VALID = {"NO_CONTENT", "SPAN_ONLY", "EVENT_ONLY", "SPAN_AND_EVENT"}

    @pytest.fixture
    def wired(self, monkeypatch):
        logfire_mod = MagicMock()
        monkeypatch.setitem(sys.modules, "logfire", logfire_mod)
        module = MagicMock()
        monkeypatch.setitem(
            sys.modules, "opentelemetry.instrumentation.google_genai", module
        )
        monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
        telem._instrument_llm_sdks(logfire_mod)
        return os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"]

    def test_default_is_a_value_the_instrumentation_accepts(self, wired):
        assert wired in self.VALID

    def test_default_actually_captures_content(self, wired):
        assert wired != "NO_CONTENT"

    def test_default_is_not_a_boolean_string(self, wired):
        assert wired.lower() not in {"true", "false", "1", "0", "yes", "no"}
