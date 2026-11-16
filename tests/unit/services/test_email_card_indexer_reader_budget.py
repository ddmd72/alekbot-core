"""EmailCardIndexer with a reader page budget (email index v2, slice 5): a page with a scanned mail stores the scan as
`read_by_reader` when the indexer has a budget and as `scan_not_read` when it has none. The helpers of
`test_email_card_indexer.py` are imported, not copied. Invented mail (a 3-page lab scan)."""
from typing import Optional

from src.adapters.job_reader_page_budget import JobReaderPageBudget
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ExtractionOutcome
from src.domain.email import EmailPart
from src.services.email_attachment_text_service import EmailAttachmentTextService
from src.services.email_card_indexer import EmailCardIndexer
from src.services.email_card_service import EmailCardService, MailSource
from tests.support.email_card_store import FakeEmailCardStore
from tests.support.fake_email_mailbox import FakeEmailMailbox
from tests.unit.domain.email_card_fixtures import NONCE
from tests.unit.services.test_document_service_read import FakeExtractor, make
from tests.unit.services.test_email_card_indexer import CREDS, Lists, Users, Writer, mail
from tests.unit.services.test_email_card_service import PDF, Embeddings

LAB = "LAB-2025-03-0417.pdf"
SCAN = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=3, extracted_by="parser@3")
READ = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Laboratory report 0417. All values within range. " * 10,
                         pages_total=3, pages_read=3, model_calls=1, model_cost=0.002, extracted_by="reader@2")


async def card_one(budget: Optional[JobReaderPageBudget]) -> dict:
    part = EmailPart("0", LAB, "application/pdf", 5000, "H0")
    mailbox = FakeEmailMailbox({"m1": [part]}, {"H0": PDF})
    documents, _, reader, _ = make(parser=FakeExtractor("parser", SCAN), reader=FakeExtractor("reader", READ))
    service = EmailCardService(mailbox, EmailAttachmentTextService(mailbox, documents),
                               Writer(), Embeddings(), nonce=lambda: NONCE)
    store = FakeEmailCardStore()
    store.seed("user-1", "m1", text="Lab mail.", state="current")
    indexer = EmailCardIndexer(service, store, Lists(), Users(), source_factory=MailSource, reader_budget=budget)
    await indexer.card_page(credentials=CREDS, user_id="user-1", account_id="acc1",
                            mails=[mail("m1", "Please find the report attached.", attachments=[LAB])])
    doc = store.docs[("user-1", "m1")]
    return {"read": [a["read"] for a in doc["card"]["attachments"]], "reader_requests": len(reader.requests)}


async def test_with_a_budget_the_scan_is_stored_read_by_the_reader():
    budget = JobReaderPageBudget(10)
    assert await card_one(budget) == {"read": ["read_by_reader"], "reader_requests": 1}
    assert budget.used == 3


async def test_without_a_budget_the_scan_is_stored_scan_not_read():
    assert await card_one(None) == {"read": ["scan_not_read"], "reader_requests": 0}


async def test_two_mails_of_one_page_with_the_same_scan_are_read_and_booked_once():
    part = EmailPart("0", LAB, "application/pdf", 5000, "H0")
    mailbox = FakeEmailMailbox({"m1": [part], "m2": [part]}, {"H0": PDF})
    documents, _, reader, _ = make(parser=FakeExtractor("parser", SCAN), reader=FakeExtractor("reader", READ))
    service = EmailCardService(mailbox, EmailAttachmentTextService(mailbox, documents), Writer(), Embeddings(),
                               nonce=lambda: NONCE)
    store = FakeEmailCardStore()
    for email_id in ("m1", "m2"):
        store.seed("user-1", email_id, text="Lab mail.", state="current")
    budget = JobReaderPageBudget(10)
    indexer = EmailCardIndexer(service, store, Lists(), Users(), source_factory=MailSource, reader_budget=budget)
    await indexer.card_page(credentials=CREDS, user_id="user-1", account_id="acc1",
                            mails=[mail(i, "Please find the report attached.", attachments=[LAB]) for i in ("m1", "m2")])
    assert len(reader.requests) == 1 and budget.asked == 1 and budget.used == 3
    for email_id in ("m1", "m2"):
        assert store.docs[("user-1", email_id)]["card"]["attachments"][0]["read"] == "read_by_reader"
