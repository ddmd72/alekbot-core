from typing import Optional


class LLMError(Exception):
    """Base class for LLM provider errors."""

    def __init__(self, message: str, http_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class LLMRateLimitError(LLMError):
    """Provider returned 429 Rate Limit — request should be retried with a different provider."""


class LLMUnavailableError(LLMError):
    """Provider returned 503 Service Unavailable — provider is temporarily down."""
