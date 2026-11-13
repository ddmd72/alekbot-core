"""EmailIndexingService: an email already in the index is not re-processed (incremental, backfill); reindex
re-processes it but keeps its consolidation mark."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.composition.connector_registry import ConnectorRegistry
from src.domain.email import EmailClassificationResult
from src.ports.email_classifier_port import EmailClassifierPort
from src.ports.email_exclusions_port import EmailExclusionsPort
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.ports.email_provider_port import EmailProviderPort
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.services import email_indexing_service as module
from src.services.email_indexing_service import EmailIndexingService
from tests.unit.services.test_email_indexing_service import _make_credentials, _make_job, _make_meta

MARK = datetime(2026, 10, 1, 8, 30)


def _valuable(email_id: str) -> EmailClassificationResult:
    return EmailClassificationResult(
        email_id=email_id, valuable=True, category="travel", fact="Flight booked.", tags=["flight"]
    )


def _build(marks):
    gmail = AsyncMock(spec=EmailProviderPort)
    gmail.list_emails.return_value = ([_make_meta("m1"), _make_meta("m2"), _make_meta("m3")], None)
    gmail.batch_get_full_content.return_value = {}
    repo = AsyncMock(spec=IndexedEmailRepository)
    repo.get_indexing_state.return_value = None
    repo.get_consolidation_marks.return_value = marks
    repo.save_batch.side_effect = lambda emails: len(emails)
    exclusions = AsyncMock(spec=EmailExclusionsPort)
    exclusions.get_exclusions.return_value = []
    classifier = AsyncMock(spec=EmailClassifierPort)
    classifier.classify_batch.side_effect = lambda emails, *a, **k: [_valuable(e.email_id) for e in emails]
    embedding = AsyncMock(spec=EmbeddingService)
    embedding.get_embeddings_batch.return_value = [[0.1] * 4, [0.2] * 4, [0.3] * 4]
    service = EmailIndexingService(
        gmail=gmail, email_repo=repo, job_repo=AsyncMock(spec=EmailIndexingJobRepository),
        exclusions_repo=exclusions, classifier=classifier, embedding=embedding, oauth=None,
        alert_sink=None, connector=ConnectorRegistry().get("gmail"),
    )
    return service, repo, classifier


class _Lines:
    def __init__(self):
        self.info_lines = []

    def info(self, msg, *a, **k):
        self.info_lines.append(str(msg))

    def debug(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


@pytest.mark.parametrize("mode", ["incremental", "backfill"])
async def test_stored_emails_are_not_classified_or_saved(mode, monkeypatch):
    lines = _Lines()
    monkeypatch.setattr(module, "logger", lines)
    service, repo, classifier = _build({"m1": None, "m3": MARK})
    job = _make_job()

    await service.run_indexing_job(job, _make_credentials(), "acct-1", mode=mode,
                                   backfill_until=datetime(2026, 1, 1, tzinfo=timezone.utc))

    repo.get_consolidation_marks.assert_awaited_once_with("user-abc", ["m1", "m2", "m3"])
    classified = classifier.classify_batch.await_args.args[0]
    assert [e.email_id for e in classified] == ["m2"]
    saved = repo.save_batch.await_args.args[0]
    assert [e.email_id for e in saved] == ["m2"]
    assert saved[0].consolidated_at is None
    assert job.emails_stored == 1
    assert any("skipped 2 already-indexed" in line for line in lines.info_lines)


async def test_nothing_stored_means_no_skip_line(monkeypatch):
    lines = _Lines()
    monkeypatch.setattr(module, "logger", lines)
    service, _, classifier = _build({})

    await service.run_indexing_job(_make_job(), _make_credentials(), "acct-1", mode="incremental")

    assert len(classifier.classify_batch.await_args.args[0]) == 3
    assert not any("already-indexed" in line for line in lines.info_lines)


async def test_reindex_reprocesses_all_and_keeps_the_stored_marks():
    service, repo, classifier = _build({"m1": MARK, "m2": None})

    await service.run_indexing_job(_make_job(), _make_credentials(), "acct-1", mode="reindex")

    assert [e.email_id for e in classifier.classify_batch.await_args.args[0]] == ["m1", "m2", "m3"]
    saved = {e.email_id: e for e in repo.save_batch.await_args.args[0]}
    assert set(saved) == {"m1", "m2", "m3"}
    assert saved["m1"].consolidated_at == MARK
    assert saved["m2"].consolidated_at is None
    assert saved["m3"].consolidated_at is None
