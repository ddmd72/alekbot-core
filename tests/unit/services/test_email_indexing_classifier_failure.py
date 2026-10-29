"""EmailIndexingService: a classifier failure counts as failed and alerts once; noise does not."""

from datetime import datetime, timezone
from typing import List
from unittest.mock import AsyncMock

import pytest

from src.domain.email import EmailClassificationResult
from src.ports.alert_sink import AlertSinkPort
from src.ports.email_classifier_port import EmailClassifierPort
from src.ports.email_exclusions_port import EmailExclusionsPort
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.ports.email_provider_port import EmailProviderPort
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.services.email_indexing_service import EmailIndexingService
from tests.unit.services.test_email_indexing_service import (
    _make_credentials,
    _make_job,
    _make_meta,
)


class FakeAlertSink(AlertSinkPort):
    def __init__(self, fail: bool = False) -> None:
        self.posts: List[str] = []
        self._fail = fail

    async def post(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("webhook down")
        self.posts.append(text)


def _result(email_id: str, failure=None) -> EmailClassificationResult:
    return EmailClassificationResult(
        email_id=email_id, valuable=False, category=None, fact=None, tags=[], failure=failure
    )


def _service(results, sink):
    gmail = AsyncMock(spec=EmailProviderPort)
    gmail.list_emails.return_value = ([_make_meta("e1"), _make_meta("e2")], None)
    repo = AsyncMock(spec=IndexedEmailRepository)
    repo.get_indexing_state.return_value = None
    repo.save_batch.return_value = 0
    exclusions = AsyncMock(spec=EmailExclusionsPort)
    exclusions.get_exclusions.return_value = []
    classifier = AsyncMock(spec=EmailClassifierPort)
    classifier.classify_batch.return_value = results
    return EmailIndexingService(
        gmail=gmail, email_repo=repo, job_repo=AsyncMock(spec=EmailIndexingJobRepository),
        exclusions_repo=exclusions, classifier=classifier,
        embedding=AsyncMock(spec=EmbeddingService), oauth=None, alert_sink=sink,
    )


async def test_failed_page_counts_failed_and_alerts_once_without_content():
    sink = FakeAlertSink()
    service = _service([_result("e1", "parse_error"), _result("e2", "parse_error")], sink)
    job = _make_job()

    await service.run_indexing_job(job, _make_credentials(), "account-abc")

    assert job.emails_failed == 2
    assert len(sink.posts) == 1
    text = sink.posts[0]
    assert "page_emails=2" in text and "parse_error" in text
    assert "job-123"[:8] in text and "user-abc"[:8] in text
    assert "Booking confirmed" not in text and "example" not in text


async def test_all_noise_page_does_not_alert_or_count():
    sink = FakeAlertSink()
    service = _service([_result("e1"), _result("e2")], sink)
    job = _make_job()

    await service.run_indexing_job(job, _make_credentials(), "account-abc")

    assert job.emails_failed == 0
    assert sink.posts == []


async def test_alert_delivery_failure_does_not_break_the_job():
    service = _service([_result("e1", "max_turns"), _result("e2", "max_turns")], FakeAlertSink(fail=True))
    job = _make_job()

    await service.run_indexing_job(job, _make_credentials(), "account-abc")

    assert job.emails_failed == 2
    assert job.status != "failed"


async def test_failed_page_without_sink_still_counts():
    service = _service([_result("e1", "max_turns"), _result("e2")], None)
    job = _make_job()

    await service.run_indexing_job(job, _make_credentials(), "account-abc")

    assert job.emails_failed == 1
