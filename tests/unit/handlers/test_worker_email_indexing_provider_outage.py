"""A model-provider outage during an email-indexing page is retried by Cloud Tasks, not recorded as a failed page
(roadmap 14b-R1). The page's job stays `running`, `/worker` answers 503, the claim is released, the redelivery
classifies the same page; a content/parse failure of the classifier keeps marking the batch failed.

Same production graph and Gmail fake as `test_worker_redelivery_email_indexing.py`; its helpers are reused.
"""
from typing import List, Optional

import pytest

from src.domain.email import EmailClassificationResult, EmailMetadata
from src.domain.exceptions import (
    PROVIDER_OUTAGE_TYPES, BothProvidersUnavailableError, LLMAccountError, LLMClientError, LLMRateLimitError,
    LLMUnavailableError, TranscriptLockedError,
)
from src.domain.oauth_credential import OAuthCredential
from src.ports.email_classifier_port import EmailClassifierPort
from tests.support import http_boundary_fake
from tests.support.prod_graph import USER_ID, prod_graph  # noqa: F401  (fixture)
from tests.support.redelivery_world import deliver_with_retry
from tests.unit.handlers.test_worker_redelivery_email_indexing import (
    PAYLOAD, ValuableClassifier, _claim_docs, _email_doc_ids, _gmail, _job, _running_job,
)

pytestmark = pytest.mark.slow


class _DownThenUp(EmailClassifierPort):
    """Raises `error` on the first batch, then classifies like `ValuableClassifier`."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def classify_batch(self, emails: List[EmailMetadata], user_id: str,
                             credentials: Optional[OAuthCredential] = None, *,
                             account_id: str = "") -> List[EmailClassificationResult]:
        self.calls += 1
        if self.calls == 1:
            raise self.error
        return await ValuableClassifier().classify_batch(emails, user_id, credentials, account_id=account_id)


class _AllFailed(EmailClassifierPort):
    """The agent's answer to a content/parse error: every email carries a failure."""

    async def classify_batch(self, emails: List[EmailMetadata], user_id: str,
                             credentials: Optional[OAuthCredential] = None, *,
                             account_id: str = "") -> List[EmailClassificationResult]:
        return [EmailClassificationResult(email_id=e.email_id, valuable=False, category="", fact="",
                                          tags=[], failure="JSONDecodeError") for e in emails]


OUTAGES = [
    BothProvidersUnavailableError("primary", "fallback", LLMUnavailableError("503")),
    LLMUnavailableError("503"),
    LLMRateLimitError("429"),
    TranscriptLockedError("primary", LLMUnavailableError("503"), 2),
]


@pytest.mark.parametrize("outage", OUTAGES, ids=lambda e: type(e).__name__)
async def test_an_outage_answers_503_and_the_redelivery_indexes_the_same_page(prod_graph, monkeypatch, outage):
    fake = http_boundary_fake.install(monkeypatch)
    _gmail(fake, {None: {"messages": [{"id": "m1"}, {"id": "m2"}]}})
    service = await _running_job(prod_graph, monkeypatch)
    service._classifier = _DownThenUp(outage)

    outcomes = await deliver_with_retry(prod_graph.app.worker_handler, PAYLOAD, "task-1", attempts=2)

    assert [o[1] if isinstance(o, tuple) else o for o in outcomes][0] == 503
    assert outcomes[1] == ({"status": "ok", "has_more": False}, 200)
    assert service._classifier.calls == 2
    assert _email_doc_ids(service) == [f"{USER_ID}_m1", f"{USER_ID}_m2"]
    job = _job(service)
    assert job["status"] == "completed" and job.get("emails_failed", 0) == 0


async def test_an_outage_leaves_the_job_running_with_nothing_stored_and_the_claim_released(prod_graph, monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)
    _gmail(fake, {None: {"messages": [{"id": "m1"}, {"id": "m2"}]}})
    service = await _running_job(prod_graph, monkeypatch)
    service._classifier = _DownThenUp(OUTAGES[0])

    outcomes = await deliver_with_retry(prod_graph.app.worker_handler, PAYLOAD, "task-1", attempts=1)

    assert isinstance(outcomes[0], tuple) and outcomes[0][1] == 503
    job = _job(service)
    assert job["status"] == "running" and job.get("emails_failed", 0) == 0
    assert _email_doc_ids(service) == []
    assert _claim_docs(prod_graph) == []


async def test_a_content_failure_of_the_classifier_still_marks_the_emails_failed(prod_graph, monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)
    _gmail(fake, {None: {"messages": [{"id": "m1"}, {"id": "m2"}]}})
    service = await _running_job(prod_graph, monkeypatch)
    service._classifier = _AllFailed()

    outcomes = await deliver_with_retry(prod_graph.app.worker_handler, PAYLOAD, "task-1", attempts=2)

    assert outcomes == [({"status": "ok", "has_more": False}, 200)]
    assert _job(service)["emails_failed"] == 2


def test_a_client_error_is_not_a_provider_outage():
    for error in (LLMClientError("400"), LLMAccountError("credit")):
        assert not isinstance(error, PROVIDER_OUTAGE_TYPES)
