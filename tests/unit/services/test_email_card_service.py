"""EmailCardService (slice 3): prepare, write, build, embed for one mail, over the real attachment service and document
service (scripted parser), an in-memory mailbox that records its calls, a scripted writer and a recording embedding fake.
Every outcome of RFC 6.3 is a result, never an exception. Invented cast (the Rome booking RM-55120)."""
import time
from dataclasses import replace
from datetime import datetime
from typing import List, Optional

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ExtractionOutcome, ParserBusy
from src.domain.email import EmailPart
from src.domain.email_card import CardState
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_reply import CardWriterReply, CardWriterStatus
from src.ports.email_card_writer_port import EmailCardWriterPort
from src.ports.embedding_service import EmbeddingService
from src.services.email_attachment_text_service import EmailAttachmentTextService
from src.services.email_card_service import EmailCardService, MailSource
from tests.support.fake_email_mailbox import FakeEmailMailbox
from tests.support.oauth_credentials_store import creds
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, NONCE, PEOPLE, ZONE, answer_json
from tests.unit.services.test_document_service_read import FakeExtractor, make

CREDS = creds("user-1", "gmail")
MSG = "msg_rome01"
PDF = b"%PDF-1.7\n" + b"booking" * 40
TEXT = ExtractionOutcome(status=ExtractionStatus.TEXT, pages_total=1, extracted_by="parser@3",
                         text="Booking RM-55120 at Albergo Immaginario, Via Finta 7, Rome. 10–14 Aug 2025. 560 EUR. " * 12)


class RecordingMailbox(FakeEmailMailbox):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.listed: List[str] = []

    async def list_parts(self, credentials, message_id):
        self.listed.append(message_id)
        return await super().list_parts(credentials, message_id)


class ScriptedWriter(EmailCardWriterPort):
    def __init__(self, status: CardWriterStatus = CardWriterStatus.OK, raw: Optional[str] = None) -> None:
        self.status, self.raw = status, raw if raw is not None else answer_json()
        self.inputs: List[str] = []

    async def write(self, account_id, static_block, card_input_text):
        self.inputs.append(card_input_text)
        answer = parse_mail_answer(self.raw) if self.status is CardWriterStatus.OK else None
        return CardWriterReply(self.status, answer, "gemini-3.5-flash-lite", 0.0004, 1)


class Embeddings(EmbeddingService):
    def __init__(self, fail: bool = False) -> None:
        self.texts: List[str] = []
        self.fail = fail

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("embedding down")
        return [0.1, 0.2, 0.3]

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [await self.get_embedding(t, task_type) for t in texts]


def source(**over) -> MailSource:
    base = dict(email_id=MSG, from_header="Albergo Immaginario <booking@albergo.example>",
                subject="Prenotazione confermata RM-55120", sent_at=datetime(2025, 6, 2, 9, 30),
                recipients=("alex@example.com",), body=BOOKING_BODY, attachment_names=("RM-55120.pdf",))
    base.update(over)
    return MailSource(**base)


class Rig:
    def __init__(self, *, parser=None, messages=None, writer=None, embeddings=None) -> None:
        part = EmailPart("0", "RM-55120.pdf", "application/pdf", 5000, "H0")
        self.mailbox = RecordingMailbox({MSG: [part]} if messages is None else messages, {"H0": PDF})
        documents, self.parser, _, _ = make(parser=parser or FakeExtractor("parser", TEXT))

        async def no_sleep(_: float) -> None:
            return None

        attachments = EmailAttachmentTextService(self.mailbox, documents, sleep=no_sleep)
        self.writer = writer or ScriptedWriter()
        self.embeddings = embeddings or Embeddings()
        self.service = EmailCardService(self.mailbox, attachments, self.writer, self.embeddings, nonce=lambda: NONCE)

    async def card(self, src: Optional[MailSource] = None):
        return await self.service.card(CREDS, "acc1", src or source(), zone=ZONE, owner_addresses=["alex@example.com"],
                                       static_block="BLOCK", people=PEOPLE, deadline_at=time.time() + 600, cache={})


async def test_a_booking_mail_with_its_pdf_is_carded_keyed_and_embedded():
    rig = Rig()
    result = await rig.card()
    assert result.state is CardState.CARDED and result.reason == "" and not result.charged_failure
    assert result.card is not None and "v:rm55120" in result.card.about_keys
    assert [(a.name, a.read) for a in result.card.attachments] == [("RM-55120.pdf", "text")]
    assert result.vector == [0.1, 0.2, 0.3] and len(rig.embeddings.texts) == 1
    assert "RM-55120.pdf" in rig.writer.inputs[0] and "Via Finta 7" in rig.writer.inputs[0]


async def test_a_mail_without_attachment_names_makes_no_list_parts_call():
    rig = Rig()
    result = await rig.card(source(attachment_names=()))
    assert result.state is CardState.CARDED and rig.mailbox.listed == [] and rig.parser.requests == []


async def test_an_invalid_answer_leaves_the_card_pending_and_charged():
    result = await Rig(writer=ScriptedWriter(CardWriterStatus.INVALID)).card()
    assert (result.state, result.charged_failure, result.reason) == (CardState.PENDING, True, "invalid")
    assert result.card is None and result.vector is None and result.reply is not None
    refused = await Rig(writer=ScriptedWriter(CardWriterStatus.REFUSED)).card()
    assert (refused.state, refused.charged_failure, refused.reason) == (CardState.PENDING, True, "refused")


async def test_an_outage_leaves_the_card_pending_and_not_charged():
    result = await Rig(writer=ScriptedWriter(CardWriterStatus.UNAVAILABLE)).card()
    assert (result.state, result.charged_failure, result.reason) == (CardState.PENDING, False, "outage")


async def test_a_parser_busy_forever_leaves_the_mail_pending_without_calling_the_writer():
    rig = Rig(parser=FakeExtractor("parser", ParserBusy()))
    result = await rig.card()
    assert (result.state, result.charged_failure, result.reason) == (CardState.PENDING, False, "attachments")
    assert rig.writer.inputs == [] and result.reply is None


async def test_an_unknown_message_is_source_gone():
    rig = Rig(messages={})
    result = await rig.card()
    assert (result.state, result.charged_failure, result.reason) == (CardState.SOURCE_GONE, False, "gone")
    assert rig.writer.inputs == []


async def test_an_empty_card_is_stored_without_an_embedding():
    rig = Rig(writer=ScriptedWriter(raw=answer_json(substance="none")))
    result = await rig.card()
    assert result.state is CardState.EMPTY and result.card is not None and result.vector is None
    assert rig.embeddings.texts == []


async def test_an_embedding_failure_keeps_the_card_and_leaves_the_vector_empty():
    result = await Rig(embeddings=Embeddings(fail=True)).card()
    assert result.state is CardState.CARDED and result.card is not None and result.vector is None


async def test_the_owner_is_a_direct_recipient_when_cc_holds_the_address():
    rig = Rig()
    await rig.card(source(recipients=("clara@example.com", "Alex@Example.com")))
    assert "Owner is a direct recipient: yes" in rig.writer.inputs[0]
    await rig.card(source(recipients=("clara@example.com",)))
    assert "Owner is a direct recipient: no" in rig.writer.inputs[1]


async def test_prepare_and_finish_are_the_shared_part():
    rig = Rig()
    prepared = await rig.service.prepare(CREDS, "acc1", source(), zone=ZONE, owner_addresses=["alex@example.com"],
                                         deadline_at=time.time() + 600, cache={})
    card = rig.service.finish(prepared, parse_mail_answer(answer_json()), PEOPLE)
    assert card.mail == prepared.mail and card.attachments == prepared.attachments
    assert await rig.service.embed(card) == [0.1, 0.2, 0.3]
