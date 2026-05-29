"""Unit tests for EmailEmbeddingRepairService (R13.1).

Pins the repair contract:
- empty pending list short-circuits with zero count, no embedding calls
- pending emails get all four vectors written via update_vectors
- per-email exceptions are caught and reported (do not abort the batch)
- attachments produce a fourth vector via the single-text embedding path
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.domain.email import IndexedEmail
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.services.email_embedding_repair_service import EmailEmbeddingRepairService


_NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def _make_email(email_id: str, attachments: list[str] = None) -> IndexedEmail:
    return IndexedEmail(
        email_id=email_id,
        user_id="user-1",
        account_id="acc-1",
        source="gmail",
        text=f"body of {email_id}",
        tags=["work"],
        category="work",
        metadata={"subject": f"Subject {email_id}"},
        subject=f"Subject {email_id}",
        from_address="sender@example.com",
        email_date=_NOW,
        attachments=attachments or [],
        indexed_at=_NOW,
        embedding_pending=True,
    )


def _make_service(batch_size: int = 100) -> tuple[EmailEmbeddingRepairService, AsyncMock, AsyncMock]:
    repo = AsyncMock(spec=IndexedEmailRepository)
    embedding = AsyncMock(spec=EmbeddingService)
    embedding.get_embeddings_batch = AsyncMock(
        return_value=[[0.1] * 768, [0.2] * 768, [0.3] * 768]
    )
    embedding.get_embedding = AsyncMock(return_value=[0.4] * 768)
    service = EmailEmbeddingRepairService(
        email_repo=repo,
        embedding=embedding,
        batch_size=batch_size,
    )
    return service, repo, embedding


class TestEmailEmbeddingRepairService:

    async def test_empty_pending_returns_zero(self):
        service, repo, embedding = _make_service()
        repo.get_pending_embeddings = AsyncMock(return_value=[])

        repaired = await service.run()

        assert repaired == 0
        embedding.get_embeddings_batch.assert_not_awaited()
        repo.update_vectors.assert_not_called()

    async def test_pending_emails_get_three_vectors_via_batch(self):
        service, repo, embedding = _make_service()
        repo.get_pending_embeddings = AsyncMock(return_value=[_make_email("e1")])
        repo.update_vectors = AsyncMock()

        repaired = await service.run()

        assert repaired == 1
        embedding.get_embeddings_batch.assert_awaited_once()
        repo.update_vectors.assert_awaited_once()
        args = repo.update_vectors.await_args
        # update_vectors(user_id, email_id, vectors)
        assert args.args[0] == "user-1"
        assert args.args[1] == "e1"
        vectors = args.args[2]
        assert "vector" in vectors
        assert "tags_vector" in vectors
        assert "metadata_vector" in vectors

    async def test_email_with_attachments_gets_fourth_vector(self):
        service, repo, embedding = _make_service()
        repo.get_pending_embeddings = AsyncMock(
            return_value=[_make_email("e1", attachments=["receipt.pdf"])]
        )
        repo.update_vectors = AsyncMock()

        await service.run()

        embedding.get_embedding.assert_awaited_once()
        vectors = repo.update_vectors.await_args.args[2]
        assert "attachments_vector" in vectors

    async def test_email_without_attachments_skips_fourth_vector(self):
        service, repo, embedding = _make_service()
        repo.get_pending_embeddings = AsyncMock(return_value=[_make_email("e1")])
        repo.update_vectors = AsyncMock()

        await service.run()

        embedding.get_embedding.assert_not_awaited()
        vectors = repo.update_vectors.await_args.args[2]
        assert "attachments_vector" not in vectors

    async def test_per_email_exception_does_not_abort_batch(self):
        service, repo, embedding = _make_service()
        e_ok = _make_email("e_ok")
        e_bad = _make_email("e_bad")
        repo.get_pending_embeddings = AsyncMock(return_value=[e_bad, e_ok])
        repo.update_vectors = AsyncMock()
        # Fail on the first call, succeed on the second.
        embedding.get_embeddings_batch.side_effect = [
            RuntimeError("transient API failure"),
            [[0.1] * 768, [0.2] * 768, [0.3] * 768],
        ]

        repaired = await service.run()

        assert repaired == 1  # only e_ok succeeded
        assert repo.update_vectors.await_count == 1
        assert repo.update_vectors.await_args.args[1] == "e_ok"

    async def test_batch_size_limits_get_pending_call(self):
        service, repo, embedding = _make_service(batch_size=25)
        repo.get_pending_embeddings = AsyncMock(return_value=[])

        await service.run()

        repo.get_pending_embeddings.assert_awaited_once_with(limit=25)
