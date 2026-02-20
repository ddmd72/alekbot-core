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
