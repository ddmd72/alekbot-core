import logging
import os
import sys
from .logging_context import get_log_context
from .telemetry import get_trace_ids


class AlekFormatter(logging.Formatter):
    """Human-readable formatter with optional trace context suffixes."""

    def __init__(self):
        super().__init__()
        self.log_trace_context = os.getenv("LOG_TRACE_CONTEXT", "full").lower()

    def format(self, record):
        message = record.getMessage()
        if self.log_trace_context == "clean":
            return message

        context = get_log_context()
        trace_ids = get_trace_ids()
        trace_id = trace_ids.get("trace_id") or context.get("trace_id")
        span_id = trace_ids.get("span_id")
        session_id = context.get("session_id")
        event_id = context.get("event_id")
        user_id = context.get("user_id")

        attributes = [
            ("trace_id", trace_id),
            ("session_id", session_id),
            ("span_id", span_id),
            ("event_id", event_id),
            ("user_id", user_id)
        ]
        suffix_items = [f"{key}={value}" for key, value in attributes if value]
        if not suffix_items:
            return message
        return f"{message} | {' '.join(suffix_items)}"


class _CloudContextFilter(logging.Filter):
    """
    Inject per-request context as structured labels for Cloud Logging.

    StructuredLogHandler picks up record.labels and includes them in the
    JSON payload under logging.googleapis.com/labels, making them filterable
    in Cloud Logging Console (e.g. labels.user_id="abc123").
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = get_log_context()
        trace_ids = get_trace_ids()
        record.labels = {  # type: ignore[attr-defined]
            k: v for k, v in {
                "user_id": context.get("user_id"),
                "session_id": context.get("session_id"),
                "event_id": context.get("event_id"),
                "trace_id": trace_ids.get("trace_id") or context.get("trace_id"),
            }.items() if v
        }
        return True


def setup_logger() -> logging.Logger:
    """
    Configure root logger.

    - Cloud Run (K_SERVICE is set): structured JSON → Cloud Logging.
      Severity, message, and context labels (user_id, session_id, trace_id)
      are parsed automatically by Cloud Logging. No file handler — container
      filesystem is ephemeral.

    - Local: human-readable console (INFO) + debug file (DEBUG), unchanged.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    if os.getenv("K_SERVICE"):
        # ----------------------------------------------------------------
        # Cloud Run: structured JSON stdout → Cloud Logging
        # K_SERVICE is automatically set by the Cloud Run runtime.
        # StructuredLogHandler emits one JSON object per line; Cloud Logging
        # parses severity and labels without any extra configuration.
        # ----------------------------------------------------------------
        from google.cloud.logging.handlers import StructuredLogHandler

        cloud_handler = StructuredLogHandler(stream=sys.stdout)
        cloud_handler.setLevel(logging.INFO)
        cloud_handler.addFilter(_CloudContextFilter())
        logger.addHandler(cloud_handler)
    else:
        # ----------------------------------------------------------------
        # Local: human-readable console + rolling debug file
        # ----------------------------------------------------------------
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(AlekFormatter())
        logger.addHandler(console_handler)

        file_handler = logging.FileHandler('alek_debug.log', mode='w')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s'
        ))
        logger.addHandler(file_handler)

        logger.info("📝 Debug logging enabled: alek_debug.log")

    return logger


logger = setup_logger()
