"""Only a failure that may be the file's charges its outages (final review F1-1, arbiter R3-x2-a): a provider 5xx, an
unexpected adapter error and a never-halved one-page timeout do; a rate limit (429), an account-level error (key,
permission, model id, quota), a network failure or an open breaker are the model's for everyone and never charge
`model_outages`. The classification travels on DriveModelUnavailable.charges_file, from the agent through the reader
runner to the tick."""
import pytest

from src.domain.drive_extraction import DriveModelUnavailable, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import Priority, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS
from src.domain.exceptions import (
    BothProvidersUnavailableError, LLMClientError, LLMNetworkError, LLMRateLimitError, LLMServerError,
    LLMTimeoutError, LLMUnavailableError, ProviderBreakerOpenError, TranscriptLockedError,
)
from src.infrastructure.drive_model_call import classify_model_error

from ..composition.test_drive_reader_runner_halving import _answer_after, _request, _runner
from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

SYSTEMIC = [
    LLMRateLimitError("429", http_status=429),
    LLMClientError("unauthorized", http_status=401),
    LLMClientError("forbidden", http_status=403),
    LLMClientError("no such model", http_status=404),
    LLMNetworkError("dns"),
    ProviderBreakerOpenError("anthropic"),
    BothProvidersUnavailableError("anthropic", None, LLMRateLimitError("429", http_status=429)),
    TranscriptLockedError("anthropic", LLMRateLimitError("429", http_status=429), 1),
]
FILES = [
    LLMServerError("500", http_status=500),
    LLMUnavailableError("503", http_status=503),
    LLMTimeoutError("t"),
    RuntimeError("adapter bug"),
    BothProvidersUnavailableError("anthropic", None, LLMServerError("502", http_status=502)),
]


@pytest.mark.parametrize("exc", SYSTEMIC, ids=lambda e: type(e).__name__)
def test_systemic_failures_never_charge_the_file(exc):
    mapped = classify_model_error(exc, "drive_document_reader")
    assert isinstance(mapped, DriveModelUnavailable) and mapped.charges_file is False


@pytest.mark.parametrize("exc", FILES, ids=lambda e: type(e).__name__)
def test_failures_that_may_be_the_files_charge_it(exc):
    mapped = classify_model_error(exc, "drive_document_reader")
    assert isinstance(mapped, DriveModelUnavailable) and mapped.charges_file is True


@pytest.mark.parametrize("error,charged", [(LLMRateLimitError("429", http_status=429), False),
                                           (LLMServerError("500", http_status=500), True)],
                         ids=["429", "5xx"])
async def test_the_runner_keeps_the_classification(tmp_path, error, charged):
    # window 1-10 answers before 11-11 fails: an outage cancels the windows still in flight (§24.8)
    runner, _, _ = _runner(_answer_after("1-10", {"11-11": error}), pages_total=11)
    with pytest.raises(DriveModelUnavailable) as raised:
        await runner.extract(_request(tmp_path))
    assert raised.value.charges_file is charged and raised.value.model_calls >= 2


@pytest.mark.usefixtures("admitted")
async def test_a_rate_limit_on_a_file_never_fails_it():
    w = ContentWorld()
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=30)

    def rate_limited(request):
        raise DriveModelUnavailable("429", pages_sent=20, model_calls=3, charges_file=False)

    w.reader.before = rate_limited
    doc = w.add_doc("scan.pdf", "")
    for _ in range(MAX_ATTEMPTS + 2):
        await w.settle()
        w.clock.advance(4 * 3600)
    rec = w.rec(doc)
    assert (rec.state, rec.model_outages, rec.attempts) == (RecordState.PENDING, 0, 0)
    assert rec.priority == int(Priority.FIRST_SEEN)   # kept its class: not moved behind other work
