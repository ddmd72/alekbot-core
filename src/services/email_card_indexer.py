"""
EmailCardIndexer — one page of freshly indexed mail to stored v2 cards (email index v2 §6.5; slice 6).

Public interface:
    EmailCardIndexer(cards, store, people, users, quota=None, source_factory=MailSource, clock=time.time)
        async card_page(credentials=, user_id=, account_id=, mails=) -> CardPageReport

`MailForCard` and `CardPageReport` are the domain's (`email_card_page`).
The v1 page is already saved when this runs (D2): each mail gets one compare-and-set field update with `expect = absent`.
A mail not carded here (crash, budget, outage) keeps no card fields or a `pending` card, which slice 7's scan re-cards.
Mail is carded `CARD_CONCURRENCY` at a time inside one billing scope (the embeddings; the writer bills itself). A page
spends at most `CARD_PAGE_BUDGET_S` on cards; mail not started by then is stored `pending` (reason `budget`). An OAuth
grant error propagates after the page's running mails finish (the job's auth path owns it); any other failure of one
mail is stored as `pending` (reason `error`) and the page continues. Card content is never logged.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.confirmed_people import ConfirmedPeople
from ..domain.email_card import CardState
from ..domain.email_card_lifecycle import (
    CARD_CONCURRENCY, CARD_MAIL_BUDGET_S, CARD_PAGE_BUDGET_S, CardExpect, CardWriteResult, block_digest, gone_fields,
    pending_fields, people_digest, settled_fields,
)
from ..domain.email_card_page import CardPageReport, MailForCard
from ..domain.email_card_prompt import render_card_static_block
from ..domain.email_card_source import owner_zone
from ..domain.exceptions import OAuthGrantRevokedError
from ..domain.oauth_credential import OAuthCredential
from ..ports.confirmed_people_repository import ConfirmedPeopleRepository
from ..ports.email_card_store import EmailCardStore
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.billed_scope import billed_scope
from ..utils.logger import logger
if TYPE_CHECKING:
    from .email_card_service import CardResult, EmailCardService, MailSource


# (fields to store, vector, the card state they stand for, usd spent)
_Stored = Tuple[Dict[str, Any], Optional[List[float]], str, float]


class EmailCardIndexer:

    def __init__(self, cards: "EmailCardService", store: EmailCardStore, people: ConfirmedPeopleRepository,
                 users: UserRepository, quota: Optional[QuotaService] = None, *,
                 source_factory: Callable[..., "MailSource"],
                 clock: Callable[[], float] = time.time) -> None:
        self._cards = cards
        self._store = store
        self._people = people
        self._users = users
        self._quota = quota
        self._source = source_factory   # `MailSource`, injected: services never import each other
        self._clock = clock

    async def card_page(self, *, credentials: OAuthCredential, user_id: str, account_id: str,
                        mails: Sequence[MailForCard]) -> CardPageReport:
        started = self._clock()
        people = await self._people_of(account_id)
        user = await self._users.get_user(user_id)
        zone = owner_zone(user.config.timezone) if user else "UTC"
        mailbox = credentials.account.label
        static_block = render_card_static_block(people, mailbox)
        logger.info(f"[EmailCard] static block {block_digest(static_block)[:12]}")
        deadline = started + CARD_PAGE_BUDGET_S
        gate = asyncio.Semaphore(CARD_CONCURRENCY)

        async def one(mail: MailForCard) -> Tuple[str, CardWriteResult, str, float]:
            async with gate:
                fields, vector, state, usd = await self._fields_for(
                    mail, credentials, account_id, zone, mailbox, static_block, people, deadline)
                email_id = mail.meta.email_id
                result = await self._store.write_card(user_id, email_id, fields, vector, CardExpect.absent())
                if result is not CardWriteResult.WRITTEN:
                    logger.info(f"[EmailCard] {email_id} not stored: {result.value}")
                return email_id, result, state, usd

        async with billed_scope(self._quota, account_id, "email card page"):
            outcomes = await asyncio.gather(*(one(m) for m in mails), return_exceptions=True)

        counts = {"carded": 0, "empty": 0, "pending": 0, "gone": 0, "conflicts": 0, "missing": 0}
        usd = 0.0
        errors: List[BaseException] = []
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                errors.append(outcome)
                continue
            _, result, state, spent = outcome
            usd += spent
            if result is CardWriteResult.CONFLICT:
                counts["conflicts"] += 1
            elif result is CardWriteResult.MISSING:
                counts["missing"] += 1
            else:
                counts[_COUNT_OF[state]] += 1
        report = CardPageReport(usd=usd, ms=int((self._clock() - started) * 1000), **counts)
        logger.info(
            f"[EmailCard] page user={user_id[:8]} carded={report.carded} empty={report.empty} pending={report.pending} "
            f"gone={report.gone} conflicts={report.conflicts} missing={report.missing} usd={report.usd:.4f} "
            f"ms={report.ms}")
        for err in errors:
            if isinstance(err, OAuthGrantRevokedError):
                raise err
        if errors:
            raise errors[0]
        return report

    async def _people_of(self, account_id: str) -> Optional[ConfirmedPeople]:
        try:
            return await self._people.get(account_id)
        except ValueError as exc:
            logger.error(f"[EmailCard] confirmed people list unreadable, carding without it: {type(exc).__name__}")
            return None

    async def _fields_for(self, mail: MailForCard, credentials: OAuthCredential, account_id: str, zone: str,
                          mailbox: str, static_block: str, people: Optional[ConfirmedPeople],
                          deadline: float) -> _Stored:
        meta, content = mail.meta, mail.content
        if content is None:
            return pending_fields(reason="source", failures=0, model=""), None, "pending", 0.0
        if self._clock() >= deadline:
            return pending_fields(reason="budget", failures=0, model=""), None, "pending", 0.0
        source = self._source(
            email_id=meta.email_id, from_header=meta.from_address, subject=meta.subject, sent_at=meta.date,
            recipients=content.recipients, body=content.body_text, attachment_names=tuple(content.attachments))
        try:
            result = await self._cards.card(
                credentials, account_id, source, zone=zone, owner_addresses=(mailbox,), static_block=static_block,
                people=people, deadline_at=min(self._clock() + CARD_MAIL_BUDGET_S, deadline), cache={})
        except OAuthGrantRevokedError:
            raise
        except Exception as exc:
            logger.error(f"[EmailCard] card failed for {meta.email_id}: {type(exc).__name__}")
            return pending_fields(reason="error", failures=0, model=""), None, "pending", 0.0
        return self._map(result, people)

    def _map(self, result: "CardResult", people: Optional[ConfirmedPeople]) -> _Stored:
        usd = result.reply.usd if result.reply else 0.0
        model = result.reply.model if result.reply else ""
        if result.state is CardState.SOURCE_GONE:
            return gone_fields(), None, "gone", usd
        if result.card is not None and result.state in (CardState.CARDED, CardState.EMPTY):
            now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
            fields = settled_fields(
                result.card, digest=people_digest(result.card, people), model=model, now=now,
                vector_pending=(result.state is CardState.CARDED and result.vector is None))
            return fields, result.vector, result.state.value, usd
        failures = 1 if result.charged_failure else 0
        return pending_fields(reason=result.reason, failures=failures, model=model), None, "pending", usd


_COUNT_OF: Mapping[str, str] = {"carded": "carded", "empty": "empty", "pending": "pending", "gone": "gone"}
