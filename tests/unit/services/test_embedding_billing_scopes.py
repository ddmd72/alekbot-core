"""Every place that embeds outside an agent execution opens a billing scope for its natural unit and writes the unit's
embedding tokens to the right account once (decisions/billing_embeddings.md).

The embedding doubles model the adapter: each call adds its prompt tokens to the ledger in scope.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.composition.drive_connector_tools import build_drive_connector_tools
from src.composition.drive_mcp_setup import build_drive_mcp_app
from src.domain.billing import calculate_cost, current_ledger
from src.domain.email import EmailClassificationResult, EmailMetadata, IndexedEmail, IndexingJob
from src.ports.email_classifier_port import EmailClassifierPort
from src.ports.email_exclusions_port import EmailExclusionsPort
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.ports.email_provider_port import EmailProviderPort
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.ports.quota_service import QuotaService
from src.services.companion_extraction_service import CompanionExtractionService
from src.services.drive_note_service import DriveNoteService
from src.services.email_embedding_repair_service import EmailEmbeddingRepairService
from src.services.email_indexing_service import EmailIndexingService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount

from .test_fact_profile_service import World as FactProfileWorld, CAR, ok, answer
from ..composition.test_drive_mcp_tools import (
    AUTH_CONFIG, HEADERS, Search as McpSearch, World as McpWorld, Auth as McpAuth,
)

EMBED = "gemini-embedding-2"
PER_CALL = 3
_NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


class MeteredEmbeddings(EmbeddingService):
    """A call (single or batch) costs PER_CALL tokens per text, on the ledger in scope."""

    def __init__(self) -> None:
        self.texts: List[str] = []

    def _meter(self, n: int) -> None:
        ledger = current_ledger()
        if ledger is not None:
            ledger.add(EMBED, prompt_tokens=PER_CALL * n)

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        self.texts.append(text)
        self._meter(1)
        return [0.1, 0.2]

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.texts.extend(texts)
        self._meter(len(texts))
        return [[0.1, 0.2] for _ in texts]


class RecordingQuota(QuotaService):
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def record_usage(self, account_id, model, tokens, cost):
        self.calls.append((account_id, model, tokens, cost))

    def expect(self, account_id: str, tokens: int) -> list:
        return [(account_id, EMBED, tokens, calculate_cost(EMBED, tokens, 0))]


# --- fact profile step ---------------------------------------------------------------------------------------------

async def test_the_fact_profile_step_bills_its_label_embeddings_to_the_account():
    w = FactProfileWorld()
    w.embedder = MeteredEmbeddings()
    w.service._embeddings = w.embedder
    quota = RecordingQuota()
    w.service._quota = quota
    await w.seed(CAR, age_days=2)
    w.profiler.replies[CAR] = ok(answer(label_words=["vehicle cover"]))
    await w.run()
    assert w.embedder.texts == ["vehicle cover"]
    from .test_fact_profile_service import ACC
    assert quota.calls == quota.expect(ACC, PER_CALL)


# --- email indexing page and repair ---------------------------------------------------------------------------------

def _credentials() -> OAuthCredential:
    return OAuthCredential(
        user_id="user-1", account_id="account-a", kind="gmail", integration=Integration.MAIL,
        account=ProviderAccount(realm="google", subject="", label="a@example.com"),
        access_token="t", refresh_token="r", token_expiry=datetime(2099, 1, 1, tzinfo=timezone.utc),
        scopes=["gmail.readonly"])


async def test_an_email_indexing_page_bills_its_embeddings_once():
    gmail = AsyncMock(spec=EmailProviderPort)
    gmail.batch_get_full_content.return_value = {}
    meta = [EmailMetadata(email_id=f"e{i}", provider="gmail", subject="s", from_address="x@example.net",
                          date=datetime.now(timezone.utc).replace(tzinfo=None), labels=[], snippet="")
            for i in range(2)]
    gmail.list_emails.return_value = (meta, None)
    email_repo = AsyncMock(spec=IndexedEmailRepository)
    email_repo.get_indexing_state.return_value = None
    email_repo.save_batch.return_value = 2
    exclusions = AsyncMock(spec=EmailExclusionsPort)
    exclusions.get_exclusions.return_value = []
    classifier = AsyncMock(spec=EmailClassifierPort)
    classifier.classify_batch.return_value = [
        EmailClassificationResult(email_id=m.email_id, valuable=True, category="personal", fact="text one", tags=["t"])
        for m in meta]
    quota = RecordingQuota()
    service = EmailIndexingService(
        gmail=gmail, email_repo=email_repo, job_repo=AsyncMock(spec=EmailIndexingJobRepository),
        exclusions_repo=exclusions, classifier=classifier, embedding=MeteredEmbeddings(), oauth=None,
        connector=ConnectorRegistry().get("gmail"), quota=quota)
    job = IndexingJob(job_id="job-1", user_id="user-1", provider="gmail", triggered_by="test", status="running",
                      started_at=_NOW, updated_at=_NOW)
    await service.run_indexing_job(job, _credentials(), "account-a")
    assert quota.calls == quota.expect("account-a", 2 * 3 * PER_CALL)   # two emails x one 3-text batch


def _pending(email_id: str, account_id: str) -> IndexedEmail:
    return IndexedEmail(email_id=email_id, user_id="user-1", account_id=account_id, source="gmail", text="text one",
                        tags=[], category="work", metadata={}, subject="s", from_address="x@example.net",
                        email_date=_NOW, attachments=[], indexed_at=_NOW, embedding_pending=True)


async def test_the_email_repair_bills_each_account_for_its_own_emails():
    repo = AsyncMock(spec=IndexedEmailRepository)
    repo.get_pending_embeddings.return_value = [
        _pending("e1", "account-a"), _pending("e2", "account-b"), _pending("e3", "account-a")]
    quota = RecordingQuota()
    service = EmailEmbeddingRepairService(email_repo=repo, embedding=MeteredEmbeddings(), quota=quota)
    repaired, _ = await service.run()
    assert repaired == 3
    assert sorted(quota.calls) == sorted(quota.expect("account-a", 2 * 3 * PER_CALL) + quota.expect("account-b", 3 * PER_CALL))


# --- companion extraction -------------------------------------------------------------------------------------------

async def test_companion_extraction_bills_the_records_embeddings_to_the_batch_account():
    quota = RecordingQuota()
    service = CompanionExtractionService(
        queue=MagicMock(), companion_repo=MagicMock(), cache_repo=MagicMock(), embedding_service=MeteredEmbeddings(),
        extractor=MagicMock(), quota=quota)
    batch = SimpleNamespace(batch_id="b1", session_id="s1", account_id="account-a", created_by_user_id="user-1",
                            companion_type="tutor")
    records = await service._build_records(batch, [{"text": "text one", "domain": "grammar_error"},
                                                   {"text": "text two", "domain": "grammar_error"}])
    assert len(records) == 2
    assert quota.calls == quota.expect("account-a", 2 * PER_CALL)


# --- drive note write -----------------------------------------------------------------------------------------------

async def test_a_drive_note_embedding_bills_the_writing_account(monkeypatch):
    quota = RecordingQuota()
    service = object.__new__(DriveNoteService)   # _embed reads only the embedding service and the quota service
    service._embeddings = MeteredEmbeddings()
    service._quota = quota
    content = SimpleNamespace(text="text one", is_empty=False)
    monkeypatch.setattr("src.services.drive_note_service.note_embedding_text", lambda c, n: "text one")
    vector, pending = await service._embed("user-1", "account-a", content, "name")
    assert vector == [0.1, 0.2] and pending is False
    assert quota.calls == quota.expect("account-a", PER_CALL)


# --- Cabinet fact search ---------------------------------------------------------------------------------------------

async def test_the_cabinet_fact_search_bills_the_query_embedding_to_the_session_account():
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "user-1", "account_id": "account-a",
                                                                  "role": "owner"})
    fact_repo = MagicMock()
    fact_repo.search_facts = AsyncMock(return_value=[])
    quota = RecordingQuota()
    app = Quart("test_embedding_billing")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(), fact_repo=fact_repo,
        embedding_service=MeteredEmbeddings(), quota_service=quota))
    async with app.test_client() as client:
        resp = await client.post("/api/user/facts/search", headers={"Authorization": "Bearer token"},
                                 json={"query": "text one"})
        assert resp.status_code == 200
    assert quota.calls == quota.expect("account-a", PER_CALL)


# --- drive MCP call ---------------------------------------------------------------------------------------------------

class MeteredMcpSearch(McpSearch):
    async def search(self, user_id: str, request: Any, **kw: Any):
        current_ledger().add(EMBED, prompt_tokens=PER_CALL)
        return await super().search(user_id, request, **kw)


async def test_a_drive_mcp_call_bills_its_embeddings_to_the_token_account():
    w = McpWorld()
    w.search = MeteredMcpSearch()
    quota = RecordingQuota()
    w.tools = await build_drive_connector_tools(
        conversion_service=w.conversion, drive_service=w.drive, drive_search=w.search, drive_notes=w.notes,
        token_repo=w.tokens, alert_sink=w.sink, links=object(), quota=quota)   # type: ignore[arg-type]
    w.fresh_app = lambda: build_drive_mcp_app(authorization_service=McpAuth(), auth_config=AUTH_CONFIG, tools=w.tools)
    await w.call("search_drive", query="plan")
    assert quota.calls == quota.expect("acc1", PER_CALL)
