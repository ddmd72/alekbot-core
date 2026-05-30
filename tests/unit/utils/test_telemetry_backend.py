"""
Unit tests for tracing backend selection in telemetry.init.

Covers _resolve_backend (legacy default preserved, explicit override) and
_init_logfire (token gate, Logfire configured as global provider, Cloud Trace
attached as an additional span processor for the ``both`` mode).
"""

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
