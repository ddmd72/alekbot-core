"""EmailIndexingService over the gate (email index v2 §4, slice 9b): a kept mail (forced keeps included) is stored with
headers and attachment names only, never a fact, tags, category or metadata, and reaches the card step; a dropped mail
is not stored; a failed gate call stores nothing (D4); one INFO line per answered page carries counts only. The store
is the real repository on the evaluating Firestore fake."""
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.composition.connector_registry import ConnectorRegistry
from src.domain.email import EmailClassificationResult, EmailFullContent, EmailMetadata
from src.ports.email_classifier_port import EmailClassifierPort
from src.ports.email_exclusions_port import EmailExclusionsPort
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.ports.email_provider_port import EmailProviderPort
from src.services.email_indexing_service import EmailIndexingService
from tests.support.firestore_eval import EvalDb
from tests.unit.services.test_email_indexing_service import _make_credentials, _make_job

FACTS, STATE = "eval_email_facts", "eval_email_state"
SENT = datetime(2026, 8, 3, 9, 15, tzinfo=timezone.utc)
SNIPPET = "Dear Alex Doe, your stay at Albergo Immaginario, Via Finta 7, Rome is confirmed."

PAGE = [
    EmailMetadata("msg_rome01", "gmail", "Booking RM-55120 confirmed", "Albergo Immaginario <stay@albergo.example>",
                  SENT, ["INBOX"], SNIPPET),
    EmailMetadata("msg_school02", "gmail", "Parents' meeting", "Example School <office@school.example>", SENT,
                  ["INBOX"], "The parents' meeting is on 3 March 2026 at 18:00 in the school hall."),
    EmailMetadata("msg_shop03", "gmail", "This week's offers", "Supermercado Exemplo <news@supermercado.example>",
                  SENT, ["INBOX"], "Fresh fruit at half price this week only."),
]


class ScriptedGate(EmailClassifierPort):
    def __init__(self, results: List[EmailClassificationResult]) -> None:
        self.results = results
        self.pages: List[List[str]] = []

    async def classify_batch(self, emails, user_id, *, account_id):
        self.pages.append([e.email_id for e in emails])
        return self.results


class RecordingStep:
    def __init__(self) -> None:
        self.mails = []

    async def card_page(self, *, credentials, user_id, account_id, mails):
        self.mails.extend(mails)


def _world(results):
    db = EvalDb()
    repo = FirestoreIndexedEmailRepository(db, SimpleNamespace(domain_email_facts_collection=FACTS,
                                                               email_indexing_state_collection=STATE))
    gmail = AsyncMock(spec=EmailProviderPort)        # a boundary we do not own: scripted answers only
    gmail.list_emails.return_value = (PAGE, None)
    gmail.batch_get_full_content.side_effect = lambda credentials, email_ids, **kw: {
        e: EmailFullContent(email_id=e, body_text="body", body_html=None, attachments=[f"{e}.pdf"]) for e in email_ids}
    exclusions = AsyncMock(spec=EmailExclusionsPort)
    exclusions.get_exclusions.return_value = []
    service = EmailIndexingService(gmail=gmail, email_repo=repo, job_repo=AsyncMock(spec=EmailIndexingJobRepository),
                                   exclusions_repo=exclusions, classifier=ScriptedGate(results), oauth=None,
                                   connector=ConnectorRegistry().get("gmail"))
    step = RecordingStep()
    service.attach_card_step(step)
    return service, db, step


def _stored(db):
    return {doc["email_id"]: doc for doc in db.collection(FACTS).docs.values()}


async def _run(service):
    job = _make_job()
    await service.run_indexing_job(job, _make_credentials(), "acct-1", mode="incremental")
    return job


async def test_kept_mail_is_stored_without_the_v1_fields_and_reaches_the_card_step():
    service, db, step = _world([
        EmailClassificationResult("msg_rome01", keep=True, reason="record"),
        EmailClassificationResult("msg_school02", keep=True, reason="notice"),
        EmailClassificationResult("msg_shop03", keep=False, reason="newsletter"),
    ])

    job = await _run(service)

    stored = _stored(db)
    assert sorted(stored) == ["msg_rome01", "msg_school02"] and job.emails_stored == 2
    rome = stored["msg_rome01"]
    assert not {"text", "tags", "category", "metadata"} & set(rome)
    assert rome["subject"] == "Booking RM-55120 confirmed" and rome["attachments"] == ["msg_rome01.pdf"]
    assert rome["from_address"] == "Albergo Immaginario <stay@albergo.example>" and rome["consolidated_at"] is None
    assert [m.meta.email_id for m in step.mails] == ["msg_rome01", "msg_school02"]


async def test_a_forced_keep_is_stored():
    service, db, step = _world([
        EmailClassificationResult("msg_rome01", keep=False, reason="promotion"),
        EmailClassificationResult("msg_school02", keep=False, reason="automated"),
        EmailClassificationResult("msg_shop03", keep=True, reason=None, forced=True),
    ])

    await _run(service)

    assert sorted(_stored(db)) == ["msg_shop03"] and [m.meta.email_id for m in step.mails] == ["msg_shop03"]


async def test_a_failed_gate_call_stores_nothing():
    service, db, step = _world([EmailClassificationResult(m.email_id, keep=False, failure="parse_error")
                                for m in PAGE])

    job = await _run(service)

    assert _stored(db) == {} and step.mails == [] and job.emails_failed == 3


async def test_the_page_line_has_counts_only(caplog):
    service, _, _ = _world([
        EmailClassificationResult("msg_rome01", keep=True, reason="record"),
        EmailClassificationResult("msg_school02", keep=True, reason="unsure", forced=True),
        EmailClassificationResult("msg_shop03", keep=False, reason="newsletter"),
    ])

    with caplog.at_level(logging.INFO):
        await _run(service)

    (line,) = [r.getMessage() for r in caplog.records if "[EmailGate]" in r.getMessage()]
    assert line == ("[EmailGate] page: listed=3 kept=2 dropped=1 forced=1 (record=1 correspondence=0 notice=0 "
                    "unsure=0 promotion=0 newsletter=1 social=0 sign_in=0 automated=0)")
    every_line = "\n".join(r.getMessage() for r in caplog.records)
    for leaked in ("Booking RM-55120", "Parents' meeting", "offers", SNIPPET[:20], "albergo.example", "Supermercado"):
        assert leaked not in every_line
