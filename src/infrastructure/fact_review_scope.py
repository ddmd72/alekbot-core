"""The review pass's `update_fact` and `ask_owner` (Facts S4 plan §3.5), between the review model's tool calls and the typed
write path. Built per pass from its `ReviewBatch`; it holds what the pass decided and asked.

One asyncio.Lock is the first await of every command: a turn's calls are started in emission order and the lock wakes
waiters FIFO, so they apply in the order the model wrote them (R3-14). The scope only routes: it parses with the domain's
parsers, classifies with `review_action`, refuses with the domain's texts and builds the trail reason with the domain's
builders. What it owns is the pass's state: which batch facts have a result, and which document each write left.
"""
import asyncio
import dataclasses
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Set

from ..domain.entities import ExpiryKind, FactEntity, FactState
from ..domain.exceptions import FactBornDueError, FactExpiryError, FactResolutionError
from ..domain.fact_review_ask import AskRequest, parse_ask_owner
from ..domain.fact_review_basis import ReviewBasis, parse_history, parse_payment, parse_review_basis
from ..domain.fact_review_batch import ReviewBatch
from ..domain.fact_review_invoice import InvoiceRequest
from ..domain.fact_review_outcome import ReviewDecision, review_outcome
from ..domain.fact_review_question import FactReviewQuestion
from ..domain.fact_review_quote_translation import HISTORY_TEXT_KEY, written_text_key
from ..domain.fact_review_reasons import evidence_moment, review_reason
from ..domain.fact_review_rules import (
    ASK_HELD,
    ASK_OWNER_PLACE,
    STAYS_STALE_REFUSAL,
    UPDATE_FACT_PLACE,
    UPDATES_SHAPE_REFUSAL,
    ask_refusal,
    basis_refusal,
    changed_outside_refusal,
    confirmed_expiry,
    date_passed_histories,
    fold_refusal,
    made_current_documents,
    may_write_current,
    needs_change_check,
    not_in_batch_refusal,
    not_under_review_refusal,
    payment_refusal,
    reason_plan_date,
    result_refusal,
    review_action,
)
from ..domain.fact_tool_result import (
    REFUSED_ASK,
    REFUSED_BASIS,
    REFUSED_SCOPE,
    STATUS_ASKED,
    STATUS_FAILED,
    expiry_refusal_tags,
    refused_result,
    update_result,
)
from ..domain.fact_update import ParsedFactUpdate, parse_fact_update
from ..domain.fact_write_context import FactWriteContext
from ..domain.local_time import local_date
from ..domain.pass_actor import PassActor
from ..domain.stored_fact import StoredFact
from ..ports.fact_update_port import FactUpdatePort
from ..ports.repository import FactRepository

class FactReviewScope:
    """The review pass's tool handler. `facts` reads documents (membership, evidence, the written result); `writer` is
    the typed write path, called with the review's context. `now` is the run's clock; `timezone` the owner's zone."""

    def __init__(
        self,
        batch: ReviewBatch,
        account_id: str,
        timezone: str,
        now: datetime,
        facts: FactRepository,
        writer: FactUpdatePort,
    ) -> None:
        self._batch = batch
        self._account_id = account_id
        self._tz = timezone
        self._now = now
        self._facts = facts
        self._writer = writer
        self._context = FactWriteContext.for_pass(PassActor.REVIEW, timezone, None)
        self._lock = asyncio.Lock()
        self._by_lineage: Dict[str, FactEntity] = {f.lineage_id: f for f in batch.facts}
        self._lineage_of: Dict[str, str] = {f.id: f.lineage_id for f in batch.facts}
        self._left: Dict[str, FactEntity] = dict(self._by_lineage)  # lineage -> the document the pass expects there
        self._open_by_fact = {q.fact_id: q for q in batch.questions if q.is_open}
        self._questions = {q.question_id: q for q in batch.questions}
        self._decisions: Dict[str, ReviewDecision] = {}
        self._asks: Dict[str, AskRequest] = {}
        self._folded: Dict[str, str] = {}  # a folded lineage -> the lineage of the ask that folds it
        self._invoices: Dict[str, InvoiceRequest] = {}
        self._written: Dict[str, str] = {}  # translation key -> a text this pass wrote (history, ask and payment texts)

    async def update_fact(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        """One `update_fact(fact_id, updates, basis?, history?, as_history?, payment?)` (`basis`, `history`, `as_history`
        and `payment` also accepted inside `updates`): refused with a tagged text, or written; the result is the write
        path's typed outcome rendered for the model
        (`update_result`; a failure other than an expiry refusal, with the fact changed, reads as the fact having
        changed outside the review)."""
        async with self._lock:
            return await self._update(args)

    async def ask_owner(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        """One `ask_owner(fact_id, question?, texts?, fallback, folds?, noise?)` (`fallback_date` is read only to be
        refused): refused with a tagged text, or held. The folds are held with it."""
        async with self._lock:
            return await self._ask(args)

    def kept(self) -> List[FactEntity]:
        """The batch facts with no result in this pass: neither written, asked about nor folded into an ask."""
        return [
            f for f in self._batch.facts
            if f.lineage_id not in self._decisions and f.lineage_id not in self._asks and f.lineage_id not in self._folded
        ]

    def decisions(self) -> List[ReviewDecision]:
        """The batch facts this pass wrote, in batch order, each with the outcome of its last write."""
        return [self._decisions[f.lineage_id] for f in self._batch.facts if f.lineage_id in self._decisions]

    def asks(self) -> List[AskRequest]:
        """The accepted asks, in batch order, each naming its batch fact."""
        return [self._asks[f.lineage_id] for f in self._batch.facts if f.lineage_id in self._asks]

    def invoices(self) -> List[InvoiceRequest]:
        """The routine invoices this pass rewrote as history with a `payment`, in batch order."""
        return [self._invoices[f.lineage_id] for f in self._batch.facts if f.lineage_id in self._invoices]

    def written_texts(self) -> Dict[str, str]:
        """The texts this pass wrote, by translation key (`<fact_id>:<text key>`, `written_text_key`): the history text of
        a rewrite, the texts of an ask, the payment texts."""
        return dict(self._written)

    # ------------------------------------------------------------------------------------------------ update_fact

    async def _update(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        fact_id = args.get("fact_id")
        lineage = await self._lineage(fact_id)
        if lineage is None:
            return refused_result(fact_id, REFUSED_SCOPE, not_in_batch_refusal(UPDATE_FACT_PLACE))
        taken = self._result_refusal(lineage, UPDATE_FACT_PLACE, write=True)
        if taken is not None:
            return refused_result(fact_id, REFUSED_SCOPE, taken)
        if self._left[lineage].state is FactState.CURRENT and not self._may_write_current(lineage):
            return refused_result(fact_id, REFUSED_SCOPE, not_under_review_refusal(UPDATE_FACT_PLACE))
        raw_updates = args.get("updates")
        if not isinstance(raw_updates, Mapping):
            return refused_result(fact_id, REFUSED_SCOPE, UPDATES_SHAPE_REFUSAL)
        updates = dict(raw_updates)
        if "as_history" in args and "as_history" not in updates:
            updates["as_history"] = args["as_history"]  # sent beside `updates`, it is honoured, never dropped
        raw_basis = _argument(args, updates, "basis")  # R3-10: sent inside updates, it is honoured, never dropped
        raw_history = _argument(args, updates, "history")
        raw_payment = _argument(args, updates, "payment")
        try:
            update = parse_fact_update(updates, self._tz)
        except FactExpiryError as error:
            return {
                "fact_id": fact_id, "status": STATUS_FAILED,
                **expiry_refusal_tags(isinstance(error, FactBornDueError)), "message": str(error),
            }
        except ValueError as error:
            return refused_result(fact_id, REFUSED_SCOPE, str(error))
        try:
            basis = parse_review_basis(raw_basis) if raw_basis is not None else None
            history = parse_history(raw_history)
            payment = parse_payment(raw_payment)
        except ValueError as error:
            return refused_result(fact_id, REFUSED_BASIS, str(error))
        stored = await self._resolve(fact_id, lineage)
        if stored is None:
            return refused_result(fact_id, REFUSED_SCOPE, changed_outside_refusal(UPDATE_FACT_PLACE))
        current = stored.fact
        action = review_action(current, update)
        if action is None:
            return refused_result(fact_id, REFUSED_SCOPE, STAYS_STALE_REFUSAL)
        named = await self._named(update.superseded_by if action == "supersede" else _evidence_id(basis))
        question = self._questions.get(basis.question_id) if basis is not None and basis.question_id else None
        refusal = basis_refusal(
            current, update, basis, named_fact=named, question=question, made_current=self._made_current(),
            history=history, today=local_date(self._now, self._tz), tz=self._tz, subject=self._by_lineage[lineage],
            not_evidence=set(date_passed_histories(self._decisions, self._left)),
        )
        if refusal is None:
            refusal = payment_refusal(update, payment)
        if refusal is not None:
            return refused_result(fact_id, REFUSED_BASIS, refusal)
        if _is_renewal(update, action, basis, question):
            update = dataclasses.replace(
                update, expiry=confirmed_expiry(current, self._now, self._tz), notes=(*update.notes, RENEWAL_EXPIRY_NOTE),
            )
        outcome = await self._writer.apply_fact_update(
            current.id, update, self._account_id, context=self._context,
            evidence_at=evidence_moment(basis, named, question),
            reason=review_reason(
                action, basis, named, update.superseded_by,
                reason_plan_date(current, basis, self._tz),
                as_history=update.as_history,
            ),
            history=history,
        )
        if not outcome.written:
            # An expiry refusal is about the update itself; any other may be the fact having changed outside the review.
            if needs_change_check(outcome.refusal) and not await self._unchanged(stored, lineage):
                return refused_result(fact_id, REFUSED_SCOPE, changed_outside_refusal(UPDATE_FACT_PLACE))
            return update_result(outcome)
        written = await self._facts.get_fact_by_id(outcome.written_id or "")
        if written is None:
            raise RuntimeError(f"fact {outcome.written_id} was written and cannot be read back")
        self._record(lineage, written, basis, update, payment)
        return update_result(outcome)

    async def _resolve(self, fact_id: str, lineage: str) -> Optional[StoredFact]:
        """The document `fact_id` resolves to, when it is the one the pass expects for its lineage (the batch's, or the
        one this pass's last write left), else None: the fact changed outside the review."""
        try:
            stored = await self._facts.resolve_current(fact_id, self._account_id, self._context.accepts)
        except FactResolutionError:
            return None
        expected = self._left[lineage]
        if stored is None or stored.fact.id != expected.id or stored.fact.state is not expected.state:
            return None
        return stored

    async def _unchanged(self, before: StoredFact, lineage: str) -> bool:
        """After a failed write: whether the document is still the one read before it (same id, state and change token)."""
        after = await self._resolve(before.fact.id, lineage)
        return after is not None and after.update_time == before.update_time

    def _record(
        self,
        lineage: str,
        written: FactEntity,
        basis: Optional[ReviewBasis],
        update: ParsedFactUpdate,
        payment: Optional[Mapping[str, str]],
    ) -> None:
        batch_fact = self._by_lineage[lineage]
        self._decisions[lineage] = ReviewDecision(
            batch_fact.id, review_outcome(batch_fact, written), written.id, basis.kind if basis is not None else None,
            as_history=update.as_history,
        )
        if update.content is not None:
            self._written[written_text_key(batch_fact.id, HISTORY_TEXT_KEY)] = update.content
        paid_key, not_paid_key = (written_text_key(batch_fact.id, key) for key in ("paid", "not_paid"))
        if payment is not None:
            self._invoices[lineage] = InvoiceRequest(batch_fact.id, written.id, payment["paid"], payment["not_paid"])
            self._written[paid_key], self._written[not_paid_key] = payment["paid"], payment["not_paid"]
        else:  # the last write decides
            self._invoices.pop(lineage, None)
            self._written.pop(paid_key, None)
            self._written.pop(not_paid_key, None)
        self._left[lineage] = written
        self._lineage_of[written.id] = lineage

    # ------------------------------------------------------------------------------------------------ ask_owner

    async def _ask(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            request = parse_ask_owner(args)
        except ValueError as error:
            return refused_result(args.get("fact_id") if isinstance(args, Mapping) else None, REFUSED_ASK, str(error))
        lineage = await self._lineage(request.fact_id)
        if lineage is None:
            return refused_result(request.fact_id, REFUSED_SCOPE, not_in_batch_refusal(ASK_OWNER_PLACE))
        taken = self._result_refusal(lineage, ASK_OWNER_PLACE, write=False)
        if taken is not None:
            return refused_result(request.fact_id, REFUSED_SCOPE, taken)
        fact = self._by_lineage[lineage]
        refusal = ask_refusal(
            fact, request.fallback, request.fallback_date, request.noise, self._open_by_fact.get(fact.id),
            self._now, self._tz, question=request.question, texts=request.texts,
        )
        if refusal is not None:
            return refused_result(request.fact_id, REFUSED_ASK, refusal)
        folds = await self._fold_lineages(request, lineage)
        if isinstance(folds, str):
            return refused_result(request.fact_id, REFUSED_ASK, folds)
        self._asks[lineage] = dataclasses.replace(
            request, fact_id=fact.id, folds=tuple(self._by_lineage[fl].id for fl in folds),
        )
        for fl in folds:
            self._folded[fl] = lineage
        for key, text in request.texts.items():
            self._written[written_text_key(fact.id, key)] = text
        return {"fact_id": fact.id, "status": STATUS_ASKED, "message": ASK_HELD}

    async def _fold_lineages(self, request: AskRequest, asked: str) -> "List[str] | str":
        """The lineages of the ask's folds, or the refusal text of the first fold the ask may not take: the asked fact
        itself, a fact outside the batch, or one already taken (a result, an ask, a fold or an open question)."""
        lineages: List[str] = []
        for fold in request.folds:
            fl = await self._lineage(fold)
            if fl is None:
                return fold_refusal(fold, "not_in_batch")
            if fl == asked:
                return fold_refusal(fold, "self")
            taken = (
                self._result_refusal(fl, ASK_OWNER_PLACE, write=False) is not None
                or self._by_lineage[fl].id in self._open_by_fact
            )
            if taken:
                return fold_refusal(fold, "taken")
            lineages.append(fl)
        return lineages

    # ------------------------------------------------------------------------------------------------ the pass's state

    async def _lineage(self, fact_id: Any) -> Optional[str]:
        """The batch lineage `fact_id` belongs to (a batch fact, a document this pass wrote, or another version of one),
        or None."""
        if not isinstance(fact_id, str) or not fact_id:
            return None
        lineage = self._lineage_of.get(fact_id)
        if lineage is None:
            doc = await self._facts.get_fact_by_id(fact_id)
            if doc is not None and doc.account_id == self._account_id and doc.lineage_id in self._by_lineage:
                lineage = self._lineage_of[fact_id] = doc.lineage_id
        return lineage

    def _result_refusal(self, lineage: str, place: str, *, write: bool) -> Optional[str]:
        return result_refusal(
            place, asked=lineage in self._asks or lineage in self._folded, decision=self._decisions.get(lineage), write=write,
        )

    def _may_write_current(self, lineage: str) -> bool:
        return may_write_current(self._decisions.get(lineage), self._by_lineage[lineage].id in self._open_by_fact)

    def _made_current(self) -> Set[str]:
        return set(made_current_documents(self._decisions, self._left))

    async def _named(self, fact_id: Optional[str]) -> Optional[FactEntity]:
        """The fact an evidence or successor id names: the document this pass's write left, for a lineage it wrote, else
        the stored document of that id."""
        if not fact_id:
            return None
        lineage = self._lineage_of.get(fact_id)
        if lineage is not None and lineage in self._decisions:
            return self._left[lineage]
        return await self._facts.get_fact_by_id(fact_id)


RENEWAL_EXPIRY_NOTE = "the expiry of a renewal is the confirmed window, set by code"


def _is_renewal(
    update: ParsedFactUpdate, action: str, basis: Optional[ReviewBasis], question: Optional[FactReviewQuestion],
) -> bool:
    """A rewrite on the owner's reply to a `renewed` question that carries an expiry: code sets that expiry."""
    return (
        basis is not None and basis.kind == "reply"
        and question is not None and question.question == "renewed"
        and not update.as_history
        and action == "rewrite"
        and update.expiry is not None and update.expiry.kind is not ExpiryKind.NONE
    )


def _argument(args: Mapping[str, Any], updates: Dict[str, Any], name: str) -> Any:
    """A call argument from the top level, else from inside `updates`, from which it is removed either way."""
    nested = updates.pop(name, None)
    value = args.get(name)
    return nested if value is None else value


def _evidence_id(basis: Optional[ReviewBasis]) -> Optional[str]:
    return basis.evidence_fact_id if basis is not None and basis.kind == "evidence" else None
