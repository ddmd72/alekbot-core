import hashlib
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

        # Debug capture: the Anthropic SDK emits raw HTTP request/response on the
        # ``anthropic`` logger at DEBUG when ANTHROPIC_LOG is set — but the app-wide
        # handler above is INFO, so those records would be dropped. Give the SDK logger
        # its own DEBUG handler (propagate=False, so it does not also flood the INFO
        # root handler) to surface the wire payload in Cloud Logging without lowering
        # the app-wide level. Toggle with the ANTHROPIC_LOG env var; inert otherwise.
        if os.getenv("ANTHROPIC_LOG"):
            sdk_logger = logging.getLogger("anthropic")
            sdk_logger.setLevel(logging.DEBUG)
            sdk_handler = StructuredLogHandler(stream=sys.stdout)
            sdk_handler.setLevel(logging.DEBUG)
            sdk_logger.addHandler(sdk_handler)
            sdk_logger.propagate = False
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


def quiet_third_party_loggers() -> None:
    """Silence chatty third-party loggers on every entry point.

    ``httpx``/``httpcore`` log each request at INFO with the full URL; for the Telegram
    Bot API that URL contains the bot token (``/bot<token>/sendMessage``), which would
    land in Cloud Logging. WARNING keeps real failures visible and drops the URLs.
    """
    for name in ("slack_bolt", "slack_sdk", "websocket", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def exc_brief(exc: BaseException) -> str:
    """An exception as a log line may carry it where its message must not appear: the type name and the first 12 hex
    characters of sha256(str(exc)), which tells repeats apart. A message can hold a drive path, a file name, a URL or a
    store's index-creation link with the project id (USER_DRIVE_INDEXING_RFC §27.12, Δ233)."""
    digest = hashlib.sha256(str(exc).encode("utf-8", "replace")).hexdigest()[:12]
    return f"{type(exc).__name__} (message digest {digest})"


logger = setup_logger()
