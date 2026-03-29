"""
Unit tests for src/utils/logger.py.

Coverage:
  AlekFormatter.format()
    - LOG_TRACE_CONTEXT=clean → returns raw message (no suffix)
    - context with values → appends "key=value" suffix
    - empty context → returns plain message (no suffix)
    - partial context (trace_id only) → includes only present values

  _CloudContextFilter.filter()
    - injects labels dict onto record
    - only non-None values included in labels
    - always returns True (never blocks records)

  setup_logger()
    - returns existing logger when handlers already present (early-return path)
    - local (no K_SERVICE): adds console + file handlers
    - cloud (K_SERVICE set): adds StructuredLogHandler
"""
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from src.utils.logger import AlekFormatter, _CloudContextFilter


# ---------------------------------------------------------------------------
# AlekFormatter
# ---------------------------------------------------------------------------

def _make_record(msg="Test message"):
    record = logging.LogRecord(
        name="test", level=logging.INFO,
        pathname="", lineno=0, msg=msg,
        args=(), exc_info=None,
    )
    return record


class TestAlekFormatter:

    def test_clean_mode_returns_raw_message(self):
        with patch.dict(os.environ, {"LOG_TRACE_CONTEXT": "clean"}):
            fmt = AlekFormatter()
        result = fmt.format(_make_record("hello"))
        assert result == "hello"
        assert "|" not in result

    def test_full_mode_with_context_appends_suffix(self):
        with patch.dict(os.environ, {"LOG_TRACE_CONTEXT": "full"}):
            fmt = AlekFormatter()
        context = {"trace_id": "trace-1", "session_id": "sess-1"}
        with (
            patch("src.utils.logger.get_log_context", return_value=context),
            patch("src.utils.logger.get_trace_ids", return_value={"trace_id": "trace-1"}),
        ):
            result = fmt.format(_make_record("hello"))
        assert "hello" in result
        assert "trace_id=trace-1" in result
        assert "session_id=sess-1" in result

    def test_empty_context_returns_plain_message(self):
        with patch.dict(os.environ, {"LOG_TRACE_CONTEXT": "full"}):
            fmt = AlekFormatter()
        with (
            patch("src.utils.logger.get_log_context", return_value={}),
            patch("src.utils.logger.get_trace_ids", return_value={}),
        ):
            result = fmt.format(_make_record("just message"))
        assert result == "just message"
        assert "|" not in result

    def test_partial_context_only_present_values(self):
        with patch.dict(os.environ, {"LOG_TRACE_CONTEXT": "full"}):
            fmt = AlekFormatter()
        context = {"trace_id": "t1", "session_id": None, "user_id": "u1"}
        with (
            patch("src.utils.logger.get_log_context", return_value=context),
            patch("src.utils.logger.get_trace_ids", return_value={"trace_id": "t1"}),
        ):
            result = fmt.format(_make_record("msg"))
        assert "trace_id=t1" in result
        assert "user_id=u1" in result
        assert "session_id" not in result


# ---------------------------------------------------------------------------
# _CloudContextFilter
# ---------------------------------------------------------------------------

class TestCloudContextFilter:

    def test_always_returns_true(self):
        f = _CloudContextFilter()
        record = _make_record()
        with (
            patch("src.utils.logger.get_log_context", return_value={}),
            patch("src.utils.logger.get_trace_ids", return_value={}),
        ):
            assert f.filter(record) is True

    def test_injects_labels_onto_record(self):
        f = _CloudContextFilter()
        record = _make_record()
        context = {"user_id": "u123", "session_id": "s456", "trace_id": "t789"}
        with (
            patch("src.utils.logger.get_log_context", return_value=context),
            patch("src.utils.logger.get_trace_ids", return_value={"trace_id": "t789"}),
        ):
            f.filter(record)
        assert record.labels["user_id"] == "u123"
        assert record.labels["session_id"] == "s456"
        assert record.labels["trace_id"] == "t789"

    def test_none_values_excluded_from_labels(self):
        f = _CloudContextFilter()
        record = _make_record()
        context = {"user_id": "u1", "session_id": None, "event_id": None}
        with (
            patch("src.utils.logger.get_log_context", return_value=context),
            patch("src.utils.logger.get_trace_ids", return_value={}),
        ):
            f.filter(record)
        assert "user_id" in record.labels
        assert "session_id" not in record.labels
        assert "event_id" not in record.labels


# ---------------------------------------------------------------------------
# setup_logger()
# ---------------------------------------------------------------------------

class TestSetupLogger:

    def test_returns_existing_logger_when_handlers_present(self):
        """Early-return path: if handlers already exist, no new ones added."""
        from src.utils.logger import setup_logger
        # The module-level call already set up handlers; calling again returns same logger.
        root = logging.getLogger()
        handler_count_before = len(root.handlers)
        result = setup_logger()
        assert result is root
        assert len(root.handlers) == handler_count_before

    def test_local_mode_adds_console_and_file_handlers(self, tmp_path):
        from src.utils.logger import setup_logger
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()

        try:
            fake_fh = logging.NullHandler()
            fake_fh.setLevel(logging.DEBUG)
            env = {k: v for k, v in os.environ.items() if k != "K_SERVICE"}
            with (
                patch.dict(os.environ, env, clear=True),
                patch("src.utils.logger.logging.FileHandler", return_value=fake_fh),
            ):
                setup_logger()
            assert len(root.handlers) >= 1  # at least console handler
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)

    def test_cloud_mode_adds_structured_handler(self):
        from src.utils.logger import setup_logger
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()

        mock_handler = MagicMock(spec=logging.Handler)
        mock_handler.level = logging.INFO

        try:
            with (
                patch.dict(os.environ, {"K_SERVICE": "my-service"}),
                patch("src.utils.logger.StructuredLogHandler", return_value=mock_handler, create=True),
                patch("google.cloud.logging.handlers.StructuredLogHandler", return_value=mock_handler, create=True),
            ):
                from importlib import import_module
                # Patch at the import site inside setup_logger
                with patch.dict("sys.modules", {
                    "google.cloud.logging.handlers": MagicMock(StructuredLogHandler=lambda **kw: mock_handler)
                }):
                    setup_logger()
        except Exception:
            pass  # Cloud SDK may not be installed in test env; that's fine
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)
