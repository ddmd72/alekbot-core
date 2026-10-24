"""Per-pass statistics of one consolidation loop, and the ops-alert texts built from them.

Pure data and formatting: no I/O, no LLM. Born from the Sonnet 5.5 adaptive-thinking stall
(see docs/04_solution_strategy/decisions/sonnet55_adaptive_thinking_stall.md): a pass that
runs out of turns without a final report must be loud, and while the model choice is being
watched every pass reports a compact stats line.

Privacy: the texts carry only the first 8 characters of the user id and tool NAMES/counts,
never tool arguments or fact content.
"""

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Sequence, Tuple

from .fact_lifecycle import CLOSING_STATES
from .pass_actor import PassActor

_WRITE_TOOLS = frozenset({"create_fact", "update_fact", "merge_facts"})
_CLOSING_VALUES = frozenset(state.value for state in CLOSING_STATES)
_NEW_TEXT_TOOLS = {"create_fact": "content", "merge_facts": "merged_content"}
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

# What one tool call came to. `expiry` is a refusal of a missing or malformed `expiry`; `born_due` one of a well-formed
# `expiry` that resolves already due (on a conversation pass the model may discard the candidate, so it is not an
# abandoned refusal there; on a cleanup pass the subject is a stored fact and it is).
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_EXPIRY = "expiry"
OUTCOME_BORN_DUE = "born_due"
_SHORT_NAMES = {
    "search_existing_facts": "search",
    "create_fact": "create",
    "update_fact": "update",
    "merge_facts": "merge",
    "count_words": "count",
}


def stats_alerts_enabled(raw: str) -> bool:
    """Parse the CONSOLIDATION_STATS_ALERTS value: "0", "false", "no", "off" (any case) are off."""
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _short(name: str) -> str:
    return _SHORT_NAMES.get(name, name)


def _signature(calls: Sequence[Tuple[str, Dict[str, Any]]]) -> Tuple[str, ...]:
    """Order-insensitive identity of one turn's tool calls (name + args)."""
    return tuple(sorted(
        f"{name}:{json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)}"
        for name, args in calls
    ))


@dataclass(frozen=True)
class _Outcome:
    """One call's result, in the order the pass made them. `key` is a hash of the text (create, merge) or the fact id
    (update), kept in memory only; `size` is the text length (create, merge) or 0."""

    name: str
    key: str
    outcome: str
    size: int
    closing: bool = False
    sources: FrozenSet[str] = frozenset()   # merge: its fact_ids; create: its derived_from
    words: FrozenSet[int] = frozenset()     # hashes of the text's distinct words, kept in memory only
    domain: str = ""


def _closes(name: str, args: Dict[str, Any]) -> bool:
    """An update that moves its fact to invalidated, archived or superseded."""
    updates = args.get("updates") if name == "update_fact" else None
    state = updates.get("state") if isinstance(updates, dict) else None
    return isinstance(state, str) and state.strip().lower() in _CLOSING_VALUES


def _outcome_key(name: str, args: Dict[str, Any]) -> Tuple[str, int]:
    if name in _NEW_TEXT_TOOLS:
        text = args.get(_NEW_TEXT_TOOLS[name])
        text = text if isinstance(text, str) else ""
        return hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text)
    fact_id = args.get("fact_id")
    return (fact_id if isinstance(fact_id, str) else ""), 0


def _sources(name: str, args: Dict[str, Any]) -> FrozenSet[str]:
    """The ids a create was made from (`derived_from`) or a merge joins (`fact_ids`); unreadable shapes are empty."""
    if name == "merge_facts":
        raw = args.get("fact_ids")
    elif name == "create_fact":
        attrs = args.get("fact_attributes")
        raw = attrs.get("derived_from") if isinstance(attrs, dict) else None
    else:
        return frozenset()
    if isinstance(raw, str):
        raw = [raw]
    return frozenset(i for i in raw if isinstance(i, str) and i) if isinstance(raw, (list, tuple)) else frozenset()


def _word_hashes(name: str, args: Dict[str, Any]) -> FrozenSet[int]:
    """Hashes of the content words of a created or merged text: no digit-only token, no short or filler word."""
    if name not in _NEW_TEXT_TOOLS:
        return frozenset()
    text = args.get(_NEW_TEXT_TOOLS[name])
    words = {
        w for w in (re.findall(r"\w+", text.lower()) if isinstance(text, str) else [])
        if len(w) >= _MIN_WORD_LENGTH and not w.isdigit() and w not in _FILLER_WORDS
    }
    return frozenset(int(hashlib.sha256(w.encode("utf-8")).hexdigest()[:12], 16) for w in words)


def _domain(args: Dict[str, Any]) -> str:
    attrs = args.get("fact_attributes")
    value = attrs.get("domain") if isinstance(attrs, dict) else None
    return value.strip().lower() if isinstance(value, str) else ""


def _rewrites(refused: _Outcome, later: _Outcome) -> bool:
    """Whether `later` (a successful create or merge) is the rewrite a born-due refusal asks for, which has other text.

    A merge is answered by a merge of the same non-empty set of facts. A create is answered by a create of the same
    domain that holds more than half of its content words (containment, so the outcome words a rewrite adds cost
    nothing); when it was made from facts, the answer must also be made from the same set, so a sibling split part,
    which is another concept, fails the text rule. Residual, on both sides, on purpose: a near-duplicate of the same
    subject (the same kind of event with another date or place) is taken as the answer and a dropped fact goes
    unseen; a rewrite that keeps half or fewer of the words alerts.
    """
    if later.closing or later.outcome != OUTCOME_OK:
        return False
    if refused.name == "merge_facts":
        return later.name == "merge_facts" and bool(refused.sources) and later.sources == refused.sources
    if later.name != "create_fact" or later.domain != refused.domain:
        return False
    if refused.sources and later.sources != refused.sources:
        return False
    return bool(refused.words) and len(refused.words & later.words) > _REWRITE_OVERLAP * len(refused.words)


class ConsolidationPassStats:
    """Collects what one `_run_consolidation_loop` pass did, turn by turn."""

    def __init__(
        self,
        actor: PassActor,
        model: str,
        effort: Any,
        max_turns: int,
        user_id: str,
    ) -> None:
        self.actor = PassActor(actor)
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.user_prefix = (user_id or "")[:8]
        self._turns: List[List[str]] = []
        self._signatures: List[Tuple[str, ...]] = []
        self.repeats = 0
        self._outcomes: List[_Outcome] = []
        self.error_turns = 0

    def record_turn(self, calls: Sequence[Tuple[str, Dict[str, Any]]]) -> None:
        """Record one turn's tool calls as (name, args) pairs."""
        sig = _signature(calls)
        if self._signatures and self._signatures[-1] == sig:
            self.repeats += 1
        self._signatures.append(sig)
        self._turns.append([name for name, _ in calls])

    def record_outcomes(self, calls: Sequence[Tuple[str, Dict[str, Any]]], outcomes: Sequence[str]) -> None:
        """Record what one turn's calls came to (`outcomes[i]` is the outcome of `calls[i]`; fewer outcomes than calls
        are paired from the front). A turn with any outcome other than `ok` is a tool-error turn."""
        failed = False
        for (name, args), outcome in zip(calls, outcomes):
            key, size = _outcome_key(name, args)
            self._outcomes.append(_Outcome(
                name, key, outcome, size, _closes(name, args), _sources(name, args), _word_hashes(name, args),
                _domain(args),
            ))
            failed = failed or outcome != OUTCOME_OK
        if failed:
            self.error_turns += 1

    @property
    def pass_label(self) -> str:
        return self.actor.value

    @property
    def expiry_errors(self) -> int:
        """Refused calls over `expiry`, born-due ones included."""
        return sum(1 for o in self._outcomes if o.outcome in (OUTCOME_EXPIRY, OUTCOME_BORN_DUE))

    @property
    def born_due(self) -> int:
        return sum(1 for o in self._outcomes if o.outcome == OUTCOME_BORN_DUE)

    def _abandoned(self) -> List[_Outcome]:
        """Refusals over a missing or malformed `expiry` that no later successful call answers: a create or merge
        with the same text, a non-closing update of the same fact (retiring the fact is the loss, not an answer).
        On a pass whose born-due refusal is a loss (`PassActor`) it counts the same way, and its answer (a
        past-tense rewrite, so other text) is found by `_rewrites`. Approximate on purpose: a retry of a missing-`expiry` refusal that rewrites the text
        counts as abandoned, which errs towards an alert."""
        counted = (
            (OUTCOME_EXPIRY, OUTCOME_BORN_DUE) if self.actor.born_due_refusal_is_loss else (OUTCOME_EXPIRY,)
        )
        found = []
        for i, refused in enumerate(self._outcomes):
            if refused.outcome not in counted or refused.name not in _WRITE_TOOLS:
                continue
            same_kind = (refused.name,) if refused.name == "update_fact" else tuple(_NEW_TEXT_TOOLS)
            rewritable = refused.outcome == OUTCOME_BORN_DUE and refused.name != "update_fact"
            resolved = any(
                (later.outcome == OUTCOME_OK and later.name in same_kind and later.key == refused.key
                 and not later.closing)
                or (rewritable and _rewrites(refused, later))
                for later in self._outcomes[i + 1:]
            )
            if not resolved:
                found.append(refused)
        return found

    @property
    def abandoned(self) -> int:
        return len(self._abandoned())

    @property
    def turns_used(self) -> int:
        return len(self._turns)

    @property
    def counts(self) -> Dict[str, int]:
        c: "Counter[str]" = Counter()
        for names in self._turns:
            c.update(names)
        return dict(c)

    @property
    def writes(self) -> int:
        return sum(n for name, n in self.counts.items() if name in _WRITE_TOOLS)

    def sequence(self) -> str:
        """Compact per-turn tool sequence, e.g. `t1 search×3 · t2 create×3`."""
        parts = []
        for i, names in enumerate(self._turns, start=1):
            c = Counter(_short(n) for n in names)
            body = " ".join(f"{n}×{k}" for n, k in c.items()) or "-"
            parts.append(f"t{i} {body}")
        return " · ".join(parts)

    def _counts_text(self) -> str:
        return " ".join(f"{_short(n)}×{k}" for n, k in self.counts.items()) or "none"

    def _errors_text(self) -> str:
        born = f", born due {self.born_due}" if self.born_due else ""
        return f"tool-error turns {self.error_turns} (expiry {self.expiry_errors}{born}, abandoned {self.abandoned})"

    def format_abandoned_alert(self) -> str:
        """The permanent alert for refusals of `expiry` that were never answered: the pass label,
        tool names and text lengths (or the fact id's first 8 characters), never the text."""
        parts = []
        for o in self._abandoned()[:5]:
            parts.append(f"update fact {o.key[:8]}" if o.name == "update_fact" else f"{_short(o.name)} (text {o.size} chars)")
        return (
            f"🚨 Consolidation left expiry refusals unanswered | pass `{self.pass_label}` | model {self.model} | "
            f"abandoned {self.abandoned}: {', '.join(parts)} | user {self.user_prefix}\n"
            "A refused write that is never resent saves nothing; check what the pass retired next to it."
        )

    def format_summary(self, report_ops: int, seconds: float) -> str:
        """The temporary per-pass stats line (pass ended with a final report). Turns count every
        LLM call, the final report included, because that is what MAX turns caps."""
        return (
            f"📊 Consolidation pass `{self.pass_label}` | model {self.model} | effort {self.effort} | "
            f"turns {self.turns_used + 1}/{self.max_turns} | tools: {self._counts_text()} | "
            f"repeats {self.repeats} | writes {self.writes} | {self._errors_text()} | report ops {report_ops} | "
            f"{seconds:.0f}s | user {self.user_prefix}\n{self.sequence()}"
        )

    def format_limit_alert(self, seconds: float) -> str:
        """The permanent alert: turn cap reached, no final report."""
        return (
            f"🚨 Consolidation hit the turn limit without a final report | pass `{self.pass_label}` | "
            f"model {self.model} | effort {self.effort} | turns {self.turns_used}/{self.max_turns} | "
            f"writes so far {self.writes} | repeats {self.repeats} | {self._errors_text()} | {seconds:.0f}s | "
            f"user {self.user_prefix}\n{self.sequence()}"
        )
