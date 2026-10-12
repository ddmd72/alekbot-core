"""How the drive model agents sort a failed call (USER_DRIVE_INDEXING_RFC §24.8): per input or model outage."""
import asyncio

import pytest

from src.infrastructure.drive_model_call import DriveModelCallFailed, classify_model_error
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.exceptions import (
    LLMClientError, LLMNetworkError, LLMTimeoutError, ProviderBreakerOpenError, TranscriptLockedError,
)


@pytest.mark.parametrize("exc", [LLMClientError("x", http_status=s) for s in (400, 413, 422, None)])
def test_refusals_of_the_input(exc):
    assert isinstance(classify_model_error(exc, "drive_reader"), DriveModelCallFailed)


@pytest.mark.parametrize("exc", [
    LLMClientError("x", http_status=401), LLMClientError("x", http_status=403), LLMClientError("x", http_status=404),
    LLMTimeoutError("t"), LLMNetworkError("n"), ProviderBreakerOpenError("claude"),
    TranscriptLockedError("claude", LLMNetworkError("n"), 2), asyncio.TimeoutError(), KeyError("bug"),
])
def test_everything_else_is_an_outage(exc):
    assert isinstance(classify_model_error(exc, "drive_reader"), DriveModelUnavailable)


@pytest.mark.parametrize("exc", [DriveModelCallFailed("x"), DriveModelUnavailable("y")])
def test_already_sorted_passes_through(exc):
    assert classify_model_error(exc, "drive_triage") is exc
