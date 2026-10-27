"""What follows a review pass (Facts S4 plan §3.2 step 11), decided from the batch, the account's open questions and the
pass's result: which `offers` entries go, which questions the review closes, which timed-out candidates get their
fallback, what is queued for the owner, which facts may have reached the revive alert, and the pass's counts.
Pure: it decides and writes nothing."""
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Sequence, Tuple

from .entities import FactEntity
from .fact_lifecycle_day import CounterField
from .fact_review_ask import AskRequest
from .fact_review_outcome import ReviewOutcome
from .fact_review_question import FactReviewQuestion
from .fact_review_result import FactReviewResult
from .fact_review_task import questions_about

_OUTCOME_COUNTERS: Mapping[ReviewOutcome, CounterField] = MappingProxyType({
    "revived": CounterField.REVIVED,
    "rewritten": CounterField.REWRITTEN,
    "archived": CounterField.ARCHIVED_BY_REVIEW,
    "invalidated": CounterField.INVALIDATED,
    "superseded": CounterField.SUPERSEDED,
})


@dataclass(frozen=True)
class AfterPass:
    """`decided`: the batch facts with an outcome (their `offers` entries go, `offers_removed`). `closes`: the open questions about them,
    closed by the review. `fallbacks`: the timed-out candidates the pass left undecided, each with its timed-out
    question. `asks`: each held ask with its batch fact as the pass was given it. `revived`: the documents the pass
    revived (the revive alert reads them). `counts`: the pass's numbers (`reviewed` is the batch; `closed_by_reply` the
    closes on the owner's words; `to_history` the rewrites as history, also counted as `rewritten`); a queued question
    and an applied fallback are counted when they land."""

    decided: Tuple[str, ...] = ()
    closes: Tuple[FactReviewQuestion, ...] = ()
    fallbacks: Tuple[Tuple[FactEntity, FactReviewQuestion], ...] = ()
    asks: Tuple[Tuple[FactEntity, AskRequest], ...] = ()
    revived: Tuple[str, ...] = ()
    counts: Mapping[CounterField, int] = field(default_factory=dict)

    def offers_removed(self, queued: Iterable[str]) -> Tuple[str, ...]:
        """The facts whose `offers` entry goes: the decided ones, and those of the asks whose queue write landed
        (`queued`: their fact ids) with the ask's folds (P2-2); an ask that was not queued keeps its fact's entry and its
        folds'."""
        landed = set(queued)
        removed = [*self.decided]
        for fact, ask in self.asks:
            if fact.id in landed:
                removed += [fact.id, *ask.folds]
        return tuple(dict.fromkeys(removed))


def after_pass(
    batch: Sequence[FactEntity], questions: Iterable[FactReviewQuestion], result: FactReviewResult, now: datetime,
) -> AfterPass:
    """The work after a pass over `batch` that returned `result`; `questions`: the account's open questions now.

    A timed-out candidate is a batch fact whose open question has timed out at `now` (its fact went to the model once,
    S4-D19). An outcome or an ask about a fact outside the batch is ignored: the pass decides only its batch."""
    by_id = {f.id: f for f in batch}
    outcomes = [d for d in result.outcomes if d.fact_id in by_id]
    decided = tuple(dict.fromkeys(d.fact_id for d in outcomes))
    asked = {a.fact_id for a in result.asks}
    about = questions_about(batch, questions)
    closes = tuple(q for q in about if q.fact_id in decided)
    fallbacks = tuple(
        (by_id[q.fact_id], q) for q in about
        if q.is_timed_out(now) and q.fact_id not in decided and q.fact_id not in asked
    )
    counts: Dict[CounterField, int] = {CounterField.REVIEWED: len(batch), CounterField.KEPT: len(result.kept)}
    for decision in outcomes:
        counter = _OUTCOME_COUNTERS[decision.outcome]
        counts[counter] = counts.get(counter, 0) + 1
        if decision.as_history:
            counts[CounterField.TO_HISTORY] = counts.get(CounterField.TO_HISTORY, 0) + 1
    counts[CounterField.CLOSED_BY_REPLY] = sum(1 for q in closes if q.reply_text)
    return AfterPass(
        decided=decided,
        closes=closes,
        fallbacks=fallbacks,
        asks=tuple((by_id[a.fact_id], a) for a in result.asks if a.fact_id in by_id),
        revived=tuple(d.written_id for d in outcomes if d.outcome == "revived"),
        counts=MappingProxyType(counts),
    )
