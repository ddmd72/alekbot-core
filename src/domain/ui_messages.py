"""
Platform-agnostic status types and default messages.
"""
from enum import Enum


class StatusType(Enum):
    """Semantic status types for conversation processing."""
    THINKING = "thinking"
    SEARCHING_MEMORY = "search_memory"
    SEARCHING_WEB = "search_web"
    PROCESSING_FILE = "processing_file"
    ERROR = "error"


class UIMessage(Enum):
    """Fixed single-string UI messages resolved via LocalizationPort.

    Values are keys into each locale module's UI_STRINGS dict. Entries may be
    str.format templates (e.g. UNKNOWN_COMMAND uses ``{command}``).
    """
    RESPONSE_READY = "response_ready"
    RESPONSE_TRUNCATED_SUFFIX = "response_truncated_suffix"
    EMPTY_MODEL_RESPONSE = "empty_model_response"
    UNKNOWN_COMMAND = "unknown_command"
    NEW_TOPIC_ACK = "new_topic_ack"
