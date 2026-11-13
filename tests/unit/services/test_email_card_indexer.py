"""EmailCardIndexer (slice 6): one page of saved mail to stored cards, over the real EmailCardService, a scripted card
writer, an in-test embedding service, an in-test compare-and-set store, in-memory people and user repositories and a fake
clock. Invented mail (the Albergo Immaginario booking RM-55120, an Example School notice, a Supermercado Exemplo
newsletter, a scanned FA-0137 invoice)."""
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from src.domain.confirmed_people import ConfirmedPeople
from src.domain.drive_extraction import ParserBusy
from src.domain.email import EmailFullContent, EmailMetadata, EmailPart
from src.domain.email_card_lifecycle import CardExpect, CardWriteResult
from src.domain.email_card_page import MailForCard
from src.domain.email_card_reply import CardWriterReply, CardWriterStatus
from src.domain.exceptions import OAuthGrantRevokedError
from src.ports.confirmed_people_repository import ConfirmedPeopleRepository
from src.ports.email_card_store import EmailCardStore
from src.services.email_attachment_text_service import EmailAttachmentTextService
from src.services.email_card_indexer import EmailCardIndexer
from src.services.email_card_service import EmailCardService, MailSource
from tests.support.fake_email_mailbox import FakeEmailMailbox
from tests.support.oauth_credentials_store import creds
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, NONCE, PEOPLE, answer_json
from tests.unit.services.test_document_service_read import FakeExtractor, make
from tests.unit.services.test_email_card_service import PDF, TEXT, Embeddings, ScriptedWriter

CREDS = creds("user-1", "gmail")
NEWSLETTER = "Supermercado Exemplo: this week's offers, nothing else."
SCHOOL = "Dear parents, the parents' meeting about Jamie Doe is on Tuesday 3 March 2026 at Example School."


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class CasStore(EmailCardStore):
    def __init__(self, existing: Sequence[str]) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {e: {} for e in existing}
        self.vectors: Dict[str, Optional[Sequence[float]]] = {}
        self.calls: List[str] = []

    async def read_expect(self, user_id, email_ids):
        return {}

    async def write_card(self, user_id, email_id, fields: Mapping[str, Any], vector, expect: CardExpect):
        self.calls.append(email_id)
        doc = self.docs.get(email_id)
        if doc is None:
            return CardWriteResult.MISSING
        if (doc.get("card_version"), doc.get("people_digest")) != (expect.card_version, expect.people_digest):
            return CardWriteResult.CONFLICT
        doc.update(fields)
        self.vectors[email_id] = vector
        return CardWriteResult.WRITTEN


class Lists(ConfirmedPeopleRepository):
    def __init__(self, people: Optional[ConfirmedPeople] = PEOPLE, broken: bool = False) -> None:
        self.people, self.broken = people, broken

    async def get(self, account_id):
        if self.broken:
            raise ValueError("list unreadable")
        return self.people

    async def replace(self, account_id, people, source):
        raise NotImplementedError


class Users:
    def __init__(self, zone: Optional[str] = "Europe/Lisbon") -> None:
        self.zone = zone

    async def get_user(self, user_id):
        if self.zone is None:
            return None
        from types import SimpleNamespace
        return SimpleNamespace(config=SimpleNamespace(timezone=self.zone))


class Writer(ScriptedWriter):
    """Records the static block and the calls in flight; raises what it is told to for a given input."""

    def __init__(self, *, raws: Optional[Dict[str, str]] = None, raises: Optional[Dict[str, Exception]] = None,
                 clock: Optional[Clock] = None, tick: float = 0.0, **kw) -> None:
        super().__init__(**kw)
        self.raws, self.raises, self.clock, self.tick = raws or {}, raises or {}, clock, tick
        self.blocks: List[str] = []
        self.inflight = self.peak = 0

    async def write(self, account_id, static_block, card_input_text):
        self.blocks.append(static_block)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0)
            if self.clock:
                self.clock.now += self.tick
            for needle, exc in self.raises.items():
                if needle in card_input_text:
                    raise exc
            for needle, raw in self.raws.items():
                if needle in card_input_text:
                    self.raw = raw
                    break
            else:
                self.raw = answer_json()
            return await super().write(account_id, static_block, card_input_text)
        finally:
            self.inflight -= 1


def meta(email_id: str, subject: str = "Prenotazione confermata RM-55120") -> EmailMetadata:
    return EmailMetadata(email_id=email_id, provider="gmail", subject=subject,
                         from_address="Albergo Immaginario <booking@albergo.example>",
                         date=datetime(2025, 6, 2, 9, 30), labels=[], snippet="")


def content(email_id: str, body: str = BOOKING_BODY, attachments=()) -> EmailFullContent:
    return EmailFullContent(email_id=email_id, body_text=body, body_html=None, attachments=list(attachments),
                            recipients=("alex@example.com",))


def mail(email_id: str, body: str = BOOKING_BODY, attachments=(), with_content: bool = True) -> MailForCard:
    return MailForCard(meta(email_id), content(email_id, body, attachments) if with_content else None)


class Rig:
    def __init__(self, *, ids: Sequence[str], writer: Optional[Writer] = None, parser=None, embeddings=None,
                 lists: Optional[Lists] = None, users: Optional[Users] = None, store_ids: Optional[Sequence[str]] = None,
                 messages=None) -> None:
        self.clock = Clock()
        part = EmailPart("0", "FA-0137.pdf", "application/pdf", 5000, "H0")
        mailbox = FakeEmailMailbox({i: [part] for i in ids} if messages is None else messages, {"H0": PDF})
        documents, _, _, _ = make(parser=parser or FakeExtractor("parser", TEXT))

        async def no_sleep(_: float) -> None:
            return None

        self.writer = writer or Writer(clock=self.clock)
        self.writer.clock = self.writer.clock or self.clock
        self.embeddings = embeddings or Embeddings()
        service = EmailCardService(mailbox, EmailAttachmentTextService(mailbox, documents, sleep=no_sleep),
                                   self.writer, self.embeddings, nonce=lambda: NONCE)
        self.store = CasStore(ids if store_ids is None else store_ids)
        self.indexer = EmailCardIndexer(service, self.store, lists or Lists(), users or Users(),
                                        source_factory=MailSource, clock=self.clock)

    async def page(self, mails: Sequence[MailForCard]):
        return await self.indexer.card_page(credentials=CREDS, user_id="user-1", account_id="acc1", mails=mails)


async def test_a_booking_is_carded_with_its_vector_and_the_digest():
    rig = Rig(ids=["m1"])
    report = await rig.page([mail("m1")])
    doc = rig.store.docs["m1"]
    assert (report.carded, report.pending, report.conflicts) == (1, 0, 0)
    assert doc["card_state"] == "carded" and doc["card_failures"] == 0 and doc["card_vector_pending"] is False
    assert doc["people_digest"] and doc["card_model"] == "gemini-3.5-flash-lite" and "v:rm55120" in doc["about_keys"]
    assert rig.store.vectors["m1"] == [0.1, 0.2, 0.3] and report.usd == pytest.approx(0.0004)


async def test_a_newsletter_with_nothing_printed_is_settled_empty_without_a_vector_call():
    writer = Writer(raws={"Supermercado": answer_json(substance="none")})
    rig = Rig(ids=["m1"], writer=writer)
    report = await rig.page([mail("m1", NEWSLETTER)])
    assert report.empty == 1 and rig.store.docs["m1"]["card_state"] == "empty"
    assert rig.embeddings.texts == [] and rig.store.vectors["m1"] is None


@pytest.mark.parametrize("status,failures,reason", [(CardWriterStatus.INVALID, 1, "invalid"),
                                                    (CardWriterStatus.REFUSED, 1, "refused"),
                                                    (CardWriterStatus.UNAVAILABLE, 0, "outage")])
async def test_a_writer_failure_is_stored_pending_with_the_charged_count(status, failures, reason):
    rig = Rig(ids=["m1"], writer=Writer(status=status))
    report = await rig.page([mail("m1")])
    doc = rig.store.docs["m1"]
    assert report.pending == 1
    assert (doc["card_state"], doc["card_failures"], doc["card_reason"]) == ("pending", failures, reason)


async def test_a_scanned_invoice_whose_parser_is_down_is_pending_without_a_writer_call():
    rig = Rig(ids=["m1"], parser=FakeExtractor("parser", ParserBusy()))
    await rig.page([mail("m1", "Invoice FA-0137 attached.", attachments=["FA-0137.pdf"])])
    assert rig.store.docs["m1"]["card_reason"] == "attachments" and rig.writer.blocks == []


async def test_a_message_gmail_no_longer_has_is_source_gone():
    rig = Rig(ids=["m1"], messages={})
    report = await rig.page([mail("m1", attachments=["FA-0137.pdf"])])
    assert report.gone == 1 and rig.store.docs["m1"]["card_state"] == "source_gone"


async def test_an_embedding_failure_keeps_the_card_and_marks_the_vector_pending():
    rig = Rig(ids=["m1"], embeddings=Embeddings(fail=True))
    await rig.page([mail("m1")])
    doc = rig.store.docs["m1"]
    assert doc["card_state"] == "carded" and doc["card_vector_pending"] is True and rig.store.vectors["m1"] is None


async def test_a_mail_whose_content_was_not_fetched_is_pending_source():
    rig = Rig(ids=["m1"])
    await rig.page([mail("m1", with_content=False)])
    assert rig.store.docs["m1"]["card_reason"] == "source" and rig.writer.blocks == []


async def test_mail_not_started_within_the_page_budget_is_pending_budget():
    ids = [f"m{i}" for i in range(8)]
    clock_writer = Writer(tick=100.0)
    rig = Rig(ids=ids, writer=clock_writer)
    report = await rig.page([mail(i) for i in ids])
    reasons = [rig.store.docs[i]["card_reason"] for i in ids]
    assert "budget" in reasons and reasons.count("budget") == report.pending
    assert len(clock_writer.blocks) == len(ids) - reasons.count("budget")


async def test_an_unexpected_exception_stores_error_and_the_page_continues():
    writer = Writer(raises={"Example School": RuntimeError("boom")})
    rig = Rig(ids=["m1", "m2"], writer=writer)
    report = await rig.page([mail("m1", SCHOOL), mail("m2")])
    assert rig.store.docs["m1"]["card_reason"] == "error" and rig.store.docs["m2"]["card_state"] == "carded"
    assert (report.carded, report.pending) == (1, 1)


async def test_a_revoked_grant_propagates_after_the_other_mail_finished():
    writer = Writer(raises={"Example School": OAuthGrantRevokedError("invalid_grant")})
    rig = Rig(ids=["m1", "m2"], writer=writer)
    with pytest.raises(OAuthGrantRevokedError):
        await rig.page([mail("m1", SCHOOL), mail("m2")])
    assert rig.store.docs["m2"]["card_state"] == "carded"


async def test_never_more_than_four_cards_are_in_flight():
    ids = [f"m{i}" for i in range(12)]
    rig = Rig(ids=ids)
    await rig.page([mail(i) for i in ids])
    assert rig.writer.peak == 4


async def test_the_static_block_is_the_same_text_for_every_mail_of_the_page():
    rig = Rig(ids=["m1", "m2", "m3"])
    await rig.page([mail(i) for i in ("m1", "m2", "m3")])
    assert len(set(rig.writer.blocks)) == 1 and "Jamie Doe" in rig.writer.blocks[0]
    assert "owner@example.com" in rig.writer.blocks[0]


async def test_a_broken_people_list_cards_the_mail_without_people():
    rig = Rig(ids=["m1"], lists=Lists(broken=True))
    await rig.page([mail("m1")])
    assert rig.store.docs["m1"]["card_state"] == "carded" and "Jamie Doe" not in rig.writer.blocks[0]


async def test_no_user_falls_back_to_utc():
    rig = Rig(ids=["m1"], users=Users(zone=None))
    await rig.page([mail("m1")])
    assert rig.store.docs["m1"]["card"]["mail"]["zone"] == "UTC"


async def test_a_conflict_and_a_missing_document_are_counted_not_raised():
    rig = Rig(ids=["m1", "m2", "m3"], store_ids=["m1", "m2"])
    rig.store.docs["m2"]["card_version"] = 1
    report = await rig.page([mail("m1"), mail("m2"), mail("m3")])
    assert (report.carded, report.conflicts, report.missing) == (1, 1, 1)
    assert rig.store.docs["m2"] == {"card_version": 1}
