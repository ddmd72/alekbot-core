"""
EmailCardService — one fetched mail to one graded, keyed card (email index v2 §3, §6.3; slice 3).

Public interface:
    MailSource            what the caller fetched: the metadata and full content of one mail
    PreparedMail          the code fields, the listed attachments and the model input
    CardResult            the outcome of `card` (state, card, vector, reply, whether the failure is charged, why)
    EmailCardService(provider, attachments, writer, embeddings, nonce=...)
        async prepare(credentials, account_id, source, zone=, owner_addresses=, deadline_at=, cache=) -> PreparedMail
        finish(prepared, answer, people) -> EmailCard
        async embed(card) -> Optional[List[float]]
        async card(credentials, account_id, source, ..., static_block=, people=) -> CardResult

`prepare` is the part both paths share (the online agent and the subscription script): attachments are listed and read
only when the mail names some, the input is the card core's. `card` adds the write, the build and the one vector, and
returns every outcome of RFC §6.3 as a result, never an exception: a model outage and a busy parser leave the card
`pending` and uncharged, an invalid or refused answer leaves it `pending` and charged, a deleted mail is `source_gone`.
An OAuth error propagates (the job's auth handling owns it). Nothing here stores anything.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, List, MutableMapping, Optional, Sequence, Tuple

from ..domain.confirmed_people import ConfirmedPeople
from ..domain.email_attachment import EmailAttachment
from ..domain.email_card import CardAttachment, CardState, EmailCard, MailAnswer, MailFields
from ..domain.email_card_build import build_card
from ..domain.email_card_input import CardInput, build_card_input
from ..domain.email_card_reply import CardWriterReply, CardWriterStatus
from ..domain.email_card_source import card_attachments_of, is_direct_recipient, mail_fields_of
from ..domain.email_card_text import card_embedding_text
from ..domain.exceptions import AttachmentParserUnavailable, EmailMessageGone, EmailPartUnavailable
from ..domain.oauth_credential import OAuthCredential
from ..ports.email_card_writer_port import EmailCardWriterPort
from ..ports.email_provider_port import EmailProviderPort
from ..ports.embedding_service import EmbeddingService
from ..utils.logger import logger

if TYPE_CHECKING:
    from .email_attachment_text_service import EmailAttachmentTextService


@dataclass(frozen=True)
class MailSource:
    """What the caller fetched for one mail: its metadata and full content."""
    email_id: str
    from_header: str
    subject: str
    sent_at: datetime
    recipients: Tuple[str, ...]
    body: str
    attachment_names: Tuple[str, ...]


@dataclass(frozen=True)
class PreparedMail:
    mail: MailFields
    attachments: Tuple[CardAttachment, ...]
    card_input: CardInput


@dataclass(frozen=True)
class CardResult:
    """`reason`: "" (a card was built), "outage", "attachments", "gone", "invalid" or "refused"."""
    email_id: str
    state: CardState
    card: Optional[EmailCard]
    vector: Optional[List[float]]
    reply: Optional[CardWriterReply]
    charged_failure: bool
    reason: str


class EmailCardService:

    def __init__(self, provider: EmailProviderPort, attachments: "EmailAttachmentTextService",
                 writer: EmailCardWriterPort, embeddings: EmbeddingService, *,
                 nonce: Callable[[], str] = lambda: secrets.token_hex(8)) -> None:
        self._provider = provider
        self._attachments = attachments
        self._writer = writer
        self._embeddings = embeddings
        self._nonce = nonce

    async def prepare(self, credentials: OAuthCredential, account_id: str, source: MailSource, *, zone: str,
                      owner_addresses: Sequence[str], deadline_at: float,
                      cache: MutableMapping[str, EmailAttachment]) -> PreparedMail:
        """The mail's fields, its attachments read and the model input. Raises what the provider and the attachment
        service raise (`EmailMessageGone`, `EmailPartUnavailable`, `AttachmentParserUnavailable`, OAuth errors)."""
        mail = mail_fields_of(source.email_id, source.from_header, source.subject, source.sent_at, zone)
        read: Sequence[EmailAttachment] = ()
        if source.attachment_names:
            parts = await self._provider.list_parts(credentials, source.email_id)
            read = await self._attachments.read_attachments(
                credentials, source.email_id, parts, account_id=account_id, deadline_at=deadline_at, cache=cache)
        card_attachments, texts = card_attachments_of(read)
        card_input = build_card_input(
            mail, source.body, card_attachments, texts,
            direct=is_direct_recipient(source.recipients, owner_addresses), nonce=self._nonce())
        return PreparedMail(mail=mail, attachments=card_attachments, card_input=card_input)

    def finish(self, prepared: PreparedMail, answer: MailAnswer, people: Optional[ConfirmedPeople]) -> EmailCard:
        """The graded, keyed card."""
        return build_card(answer, prepared.mail, prepared.attachments, prepared.card_input.evidence, people)

    async def embed(self, card: EmailCard) -> Optional[List[float]]:
        """The card's one vector, or None when the embedding call fails (the card is kept; the caller retries it)."""
        try:
            return list(await self._embeddings.get_embedding(card_embedding_text(card), "RETRIEVAL_DOCUMENT"))
        except Exception as exc:
            logger.error(f"[EmailCard] embedding failed for {card.mail.email_id}: {type(exc).__name__}")
            return None

    async def card(self, credentials: OAuthCredential, account_id: str, source: MailSource, *, zone: str,
                   owner_addresses: Sequence[str], static_block: str, people: Optional[ConfirmedPeople],
                   deadline_at: float, cache: MutableMapping[str, EmailAttachment]) -> CardResult:
        """One mail: prepare, write, build, embed (a `carded` card only: an `empty` one is hidden from search)."""
        email_id = source.email_id
        try:
            prepared = await self.prepare(credentials, account_id, source, zone=zone, owner_addresses=owner_addresses,
                                          deadline_at=deadline_at, cache=cache)
        except EmailMessageGone:
            return CardResult(email_id, CardState.SOURCE_GONE, None, None, None, False, "gone")
        except (AttachmentParserUnavailable, EmailPartUnavailable) as exc:
            logger.warning(f"[EmailCard] attachments unavailable for {email_id}: {type(exc).__name__}")
            return CardResult(email_id, CardState.PENDING, None, None, None, False, "attachments")
        reply = await self._writer.write(account_id, static_block, prepared.card_input.text)
        if reply.status is CardWriterStatus.UNAVAILABLE:
            return CardResult(email_id, CardState.PENDING, None, None, reply, False, "outage")
        if reply.status is not CardWriterStatus.OK or reply.answer is None:
            return CardResult(email_id, CardState.PENDING, None, None, reply, True, reply.status.value)
        card = self.finish(prepared, reply.answer, people)
        vector = await self.embed(card) if card.state is CardState.CARDED else None
        return CardResult(email_id, card.state, card, vector, reply, False, "")
