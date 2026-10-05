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
    SOURCES_HEADING = "sources_heading"
    CONSOLIDATION_STARTED = "consolidation_started"
    # A request Lelik sent during a call failed after the call stopped waiting for it.
    VOICE_REQUEST_FAILED = "voice_request_failed"
    # $skill command family (save/list/delete a custom skill).
    SKILL_USAGE = "skill_usage"
    SKILL_SAVED = "skill_saved"
    SKILL_DRAFT_NOT_FOUND = "skill_draft_not_found"
    SKILL_NOT_SAVED = "skill_not_saved"
    SKILL_LIST_HEADER = "skill_list_header"
    SKILL_LIST_EMPTY = "skill_list_empty"
    SKILL_SYSTEM_HEADER = "skill_system_header"
    SKILL_DELETED = "skill_deleted"
    SKILL_NOT_FOUND = "skill_not_found"
    SKILL_BUILT_IN = "skill_built_in"
    SKILL_UNAVAILABLE = "skill_unavailable"
    # skill_preview delivery (ConversationHandler._deliver_item) — no MessageContext there,
    # language comes from the response_channel instead (see _ui_string_for_language).
    SKILL_PREVIEW_FILE_TITLE = "skill_preview_file_title"
    SKILL_PREVIEW_DELIVERY_FAILED = "skill_preview_delivery_failed"
