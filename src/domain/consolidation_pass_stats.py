"""Per-pass statistics of one consolidation loop: what its tool calls came to, and which refusals were never answered.

Pure data: no I/O, no LLM, no text layout. Born from the Sonnet 5.5 adaptive-thinking stall
(see docs/04_solution_strategy/decisions/sonnet55_adaptive_thinking_stall.md): a pass that
runs out of turns without a final report must be loud, and while the model choice is being
watched every pass reports a compact stats line. The stats take typed calls (`PassCall`); they read no tool argument
and lay out no text.
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .entities import FactDomain
from .env_switch import is_off_value
from .fact_lifecycle_day import CounterField
from .pass_actor import PassActor

# What one tool call came to. `expiry` is a refusal of a missing or malformed `expiry`; `born_due` one of a well-formed
# `expiry` that resolves already due (on a conversation pass the model may discard the candidate, so it is not an
# abandoned refusal there; on a cleanup pass the subject is a stored fact and it is). `review_refusal` is a refusal of the
# review pass's reason or ask rules (`refused`: basis, ask): the fact is then kept, nothing is lost. `scope_refusal` is one
# of the review pass's own rules on the call (`refused`: scope: the batch, one result per fact, the state, the shape).
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_EXPIRY = "expiry"
OUTCOME_BORN_DUE = "born_due"
OUTCOME_REVIEW_REFUSAL = "review_refusal"
OUTCOME_SCOPE_REFUSAL = "scope_refusal"
_REVIEW_REFUSALS = frozenset({OUTCOME_REVIEW_REFUSAL, OUTCOME_SCOPE_REFUSAL})

# A born-due create is answered by a rewrite that holds more than this share of its content words (hashed in memory).
_REWRITE_OVERLAP = 0.5
_MIN_WORD_LENGTH = 3
# The house style opens every fact with the subject and links with these, and a past-tense rewrite always drops the
# planning words; none of them says which fact it is.
_FILLER_WORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "onto", "that", "this", "these", "those", "has", "have", "had", "was",
    "were", "are", "been", "being", "will", "would", "can", "could", "his", "her", "its", "their", "our", "not", "but",
    "user", "owner",
    "plans", "plan", "planned", "planning", "intends", "intend", "scheduled", "going", "wants", "expects", "upcoming",
    "starts", "start",
})


def stats_alerts_enabled(raw: str) -> bool:
    """Parse the CONSOLIDATION_STATS_ALERTS value: "0", "false", "no", "off" (any case) are off."""
    return not is_off_value(raw)


class PassTool(StrEnum):
    """What a pass's tool call does. A call of no known tool is OTHER."""

    SEARCH = "search"
    CREATE = "create"
    UPDATE = "update"
    MERGE = "merge"
    COUNT = "count"
    ASK = "ask"
    OTHER = "other"


_WRITES = frozenset({PassTool.CREATE, PassTool.UPDATE, PassTool.MERGE})
_NEW_TEXT = frozenset({PassTool.CREATE, PassTool.MERGE})
# The calls about one stored fact, keyed by its id: a refusal of one is answered by a later success of either.
_ON_A_FACT = frozenset({PassTool.UPDATE, PassTool.ASK})
_ANSWERABLE = _WRITES | {PassTool.ASK}


@dataclass(frozen=True)
class PassCall:
    """One tool call as the stats read it, built by the caller from the call's normalised arguments.

    `fingerprint` identifies the arguments (a repeated turn has the same ones); `key` is a hash of the text (create,
    merge, see `with_text`) or the fact id (update, ask); `size` the text length; `closing` an update that moves its fact
    to a closing state; `sources` the ids a create is made from or a merge joins; `words` hashes of the text's content
    words; `domain` the new fact's domain. The text itself is never kept.
    """

    tool: PassTool
    fingerprint: str
    key: str = ""
    size: int = 0
    closing: bool = False
    sources: FrozenSet[str] = frozenset()
    words: FrozenSet[int] = frozenset()
    domain: Optional[FactDomain] = None

    @classmethod
    def with_text(
        cls, tool: PassTool, fingerprint: str, text: str, *, sources: FrozenSet[str] = frozenset(),
        domain: Optional[FactDomain] = None,
    ) -> "PassCall":
        """A create or merge call: its key, size and words come from the new text."""
        return cls(
            tool, fingerprint, hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text), False, sources,
            _word_hashes(text), domain,
        )


def _word_hashes(text: str) -> FrozenSet[int]:
    """Hashes of the content words of a text: no digit-only token, no short or filler word."""
    words = {
        w for w in re.findall(r"\w+", text.lower())
        if len(w) >= _MIN_WORD_LENGTH and not w.isdigit() and w not in _FILLER_WORDS
    }
    return frozenset(int(hashlib.sha256(w.encode("utf-8")).hexdigest()[:12], 16) for w in words)


@dataclass(frozen=True)
class PassOutcome:
    """One call's result, in the order the pass made them."""

    call: PassCall
    outcome: str


def _rewrites(refused: PassCall, later: PassCall) -> bool:
    """Whether `later` (a successful create or merge) is the rewrite a born-due refusal asks for, which has other text.

    A merge is answered by a merge of the same non-empty set of facts. A create is answered by a create of the same
    domain that holds more than half of its content words (containment, so the outcome words a rewrite adds cost
    nothing); when it was made from facts, the answer must also be made from the same set, so a sibling split part,
    which is another concept, fails the text rule. Residual, on both sides, on purpose: a near-duplicate of the same
    subject (the same kind of event with another date or place) is taken as the answer and a dropped fact goes
    unseen; a rewrite that keeps half or fewer of the words alerts.
    """
    if later.closing:
        return False
    if refused.tool is PassTool.MERGE:
        return later.tool is PassTool.MERGE and bool(refused.sources) and later.sources == refused.sources
    if later.tool is not PassTool.CREATE or later.domain != refused.domain:
        return False
    if refused.sources and later.sources != refused.sources:
        return False
    return bool(refused.words) and len(refused.words & later.words) > _REWRITE_OVERLAP * len(refused.words)


class ConsolidationPassStats:
    """Collects what one `_run_consolidation_loop` pass did, turn by turn. `max_turns` is the pass's turn cap."""

    def __init__(self, actor: PassActor, max_turns: int) -> None:
        self.actor = PassActor(actor)
        self.max_turns = max_turns
        self._turns: List[Tuple[PassTool, ...]] = []
        self._signatures: List[Tuple[str, ...]] = []
        self.repeats = 0
        self._outcomes: List[PassOutcome] = []
        self.error_turns = 0

    def record_turn(self, calls: Sequence[PassCall]) -> None:
        """Record one turn's tool calls."""
        sig = tuple(sorted(f"{call.tool.value}:{call.fingerprint}" for call in calls))  # order-insensitive
        if self._signatures and self._signatures[-1] == sig:
            self.repeats += 1
        self._signatures.append(sig)
        self._turns.append(tuple(call.tool for call in calls))

    def record_outcomes(self, calls: Sequence[PassCall], outcomes: Sequence[str]) -> None:
        """Record what one turn's calls came to (`outcomes[i]` is the outcome of `calls[i]`; fewer outcomes than calls
        are paired from the front). A turn with any outcome other than `ok` is a tool-error turn."""
        failed = False
        for call, outcome in zip(calls, outcomes):
            self._outcomes.append(PassOutcome(call, outcome))
            failed = failed or outcome != OUTCOME_OK
        if failed:
            self.error_turns += 1

    @property
    def expiry_errors(self) -> int:
        """Refused calls over `expiry`, born-due ones included."""
        return sum(1 for o in self._outcomes if o.outcome in (OUTCOME_EXPIRY, OUTCOME_BORN_DUE))

    @property
    def born_due(self) -> int:
        return sum(1 for o in self._outcomes if o.outcome == OUTCOME_BORN_DUE)

    def abandoned_calls(self) -> List[PassCall]:
        """The calls of `abandoned_outcomes`."""
        return [o.call for o in self.abandoned_outcomes()]

    def abandoned_outcomes(self) -> List[PassOutcome]:
        """Refusals that no later successful call answers, in pass order.

        Counted: a missing or malformed `expiry`, always; a born-due one where the pass says it is a loss (`PassActor`);
        a scope refusal (a basis or ask refusal leaves the fact kept: nothing is lost). A refusal of a create or merge is
        answered by a later create or merge with the same text; one of an update or an ask by a later update or ask of the
        same fact that does not close it (retiring the fact is the loss, not an answer). On a pass that gives each fact
        one result (`PassActor.one_result_per_fact`) any successful update or ask of the fact in the pass answers it,
        before or after, closing included. A born-due create or merge is also answered by its rewrite (a past-tense one, so other text), found by
        `_rewrites`. Approximate on purpose: a retry of a missing-`expiry` refusal that rewrites the text counts as
        abandoned, which errs towards an alert."""
        counted = {OUTCOME_EXPIRY, OUTCOME_SCOPE_REFUSAL}
        if self.actor.born_due_refusal_is_loss:
            counted.add(OUTCOME_BORN_DUE)
        found: List[PassOutcome] = []
        for i, refused in enumerate(self._outcomes):
            call = refused.call
            if refused.outcome not in counted or call.tool not in _ANSWERABLE:
                continue
            one_result = self.actor.one_result_per_fact and call.tool in _ON_A_FACT
            same_kind = _ON_A_FACT if call.tool in _ON_A_FACT else _NEW_TEXT
            rewritable = refused.outcome == OUTCOME_BORN_DUE and call.tool in _NEW_TEXT
            resolved = any(
                other.outcome == OUTCOME_OK and (
                    (other.call.tool in same_kind and other.call.key == call.key
                     and (one_result or not other.call.closing))
                    or (rewritable and _rewrites(call, other.call))
                )
                for other in (self._outcomes if one_result else self._outcomes[i + 1:])
            )
            if not resolved:
                found.append(refused)
        return found

    @property
    def review_refusals(self) -> int:
        """Calls the review pass's own rules refused (basis, scope, ask)."""
        return sum(1 for o in self._outcomes if o.outcome in _REVIEW_REFUSALS)

    def day_counts(self) -> Dict[CounterField, int]:
        """What this pass adds to the account's day: its refused calls (expiry and review refusals) under the pass's
        counter (`PassActor.refusals_counter`); empty when there were none."""
        refused = self.expiry_errors + self.review_refusals
        return {self.actor.refusals_counter: refused} if refused else {}

    @property
    def abandoned(self) -> int:
        return len(self.abandoned_outcomes())

    @property
    def turns_used(self) -> int:
        return len(self._turns)

    @property
    def turns(self) -> List[Tuple[PassTool, ...]]:
        """Each turn's tools, in the order the model emitted them."""
        return list(self._turns)

    @property
    def counts(self) -> Dict[PassTool, int]:
        """Calls per tool, in first-seen order."""
        c: "Counter[PassTool]" = Counter()
        for tools in self._turns:
            c.update(tools)
        return dict(c)

    @property
    def writes(self) -> int:
        return sum(n for tool, n in self.counts.items() if tool in _WRITES)
