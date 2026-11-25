"""The memory inbox triage pass's tools (MCP_MEMORY_SUBMISSIONS_RFC §7.4), between the triage model's calls and the
decisions the triage pass service writes after the pass: `record_candidate`, `discard_submissions` and its own
`count_words` (against CANDIDATE_MAX_WORDS, not the facts' 40).

Built per pass by the triage pass service from the ids of the submissions the pass was sent, and handed to the agent
in the task's payload, so the service keeps the decisions when the agent's execution is cancelled or raises. Every call is checked before anything is held; a
refused call holds nothing and answers `{"error": "<argument>: <fix>"}`. The scope writes nothing: the agent returns
`decisions()` and the service persists them (`MemoryInboxRepository.apply_triage`).

- A candidate's ids are submissions of this pass; one submission may feed several candidates (a split), but a
  submission is never both cited and discarded: the second call is refused, either way round.
- The draft classification is parsed with the fact parsers and stored in their canonical form; it is advisory, so a
  date already past is not refused (no born-due check).
- Every relation but `new` names a current fact of this account: the id is followed to its current version
  (`resolve_current`, CONSOLIDATION_ACCEPTS) and the stored id and text are that document's, never the model's.

One asyncio.Lock is the first await of every command, as in `FactReviewScope`: a turn's calls are started in emission
order and the lock wakes waiters FIFO, so the second of two conflicting calls in one turn is the one refused.
"""
import asyncio
from typing import Any, Collection, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from ..domain.entities import ContextPriority, TemporalClass, normalize_fact_taxonomy
from ..domain.exceptions import FactExpiryError, FactResolutionError
from ..domain.fact_argument_parsing import parse_enum
from ..domain.fact_expiry import NOTHING_WRITTEN, Expiry, parse_expiry
from ..domain.fact_lifecycle import CONSOLIDATION_ACCEPTS
from ..domain.fact_words import word_count
from ..domain.memory_inbox import (
    CANDIDATE_MAX_WORDS,
    Basis,
    CandidateDraft,
    CandidateFields,
    DiscardReason,
    DraftClass,
    Relation,
    RelationKind,
    TriageDecisions,
    parse_basis_strict,
)
from ..ports.repository import FactRepository

# The reasons the model may give; `undecided` is code's own (a submission no pass decided).
_MODEL_REASONS: Tuple[DiscardReason, ...] = tuple(r for r in DiscardReason if r is not DiscardReason.UNDECIDED)
_NOT_RECORDED = "Nothing was recorded; resend the call with this fixed."


class _Refused(Exception):
    """A refused call: the argument and its fix, the text the model reads."""

    def __init__(self, argument: str, fix: str) -> None:
        super().__init__(f"{argument}: {fix}")


class MemoryInboxTriageScope:
    """The triage pass's tool handler. `submission_ids` are the submissions this pass was sent; `facts` resolves a cited
    fact of `account_id`."""

    def __init__(self, submission_ids: Collection[str], account_id: str, facts: FactRepository) -> None:
        self._sent = frozenset(submission_ids)
        self._account_id = account_id
        self._facts = facts
        self._lock = asyncio.Lock()
        self._candidates: List[CandidateDraft] = []
        self._cited: Set[str] = set()
        self._discards: Dict[str, DiscardReason] = {}

    async def record_candidate(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        """One `record_candidate(submission_ids, text, happens_when, said_when, basis, directive, temporal_class,
        context_priority, expiry, relation, fact_id)`: held, or refused naming its argument."""
        async with self._lock:
            try:
                draft = await self._candidate(args)
            except _Refused as refused:
                return {"error": f"{refused} {_NOT_RECORDED}"}
            self._candidates.append(draft)
            self._cited.update(draft.source_ids)
            return {"status": "recorded", "submission_ids": list(draft.source_ids)}

    async def discard_submissions(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        """One `discard_submissions(submission_ids, reason)`: held, or refused naming its argument. Discarding a
        submission again replaces its reason."""
        async with self._lock:
            try:
                ids = self._ids(args.get("submission_ids"))
                cited = [i for i in ids if i in self._cited]
                if cited:
                    raise _Refused("submission_ids", f"{', '.join(cited)} already feed a candidate in this pass; a "
                                   f"submission is either cited or discarded, never both. Leave them out")
                reason = _reason(args.get("reason"))
            except _Refused as refused:
                return {"error": f"{refused} {_NOT_RECORDED}"}
            for submission_id in ids:
                self._discards[submission_id] = reason
            return {"status": "discarded", "submission_ids": list(ids), "reason": reason.value}

    def count_words(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        """The word count of `text` against CANDIDATE_MAX_WORDS, by `word_count` (the rule `record_candidate` checks)."""
        text = args.get("text")
        words = word_count(text) if isinstance(text, str) else 0
        return {"word_count": words, "limit": CANDIDATE_MAX_WORDS, "within_limit": words <= CANDIDATE_MAX_WORDS,
                "excess": max(0, words - CANDIDATE_MAX_WORDS)}

    @property
    def submission_ids(self) -> FrozenSet[str]:
        """The submissions this pass was sent."""
        return self._sent

    def decisions(self) -> TriageDecisions:
        """What the pass decided so far: the candidates in the order they were recorded, and the discards."""
        return TriageDecisions(candidates=tuple(self._candidates), discards=dict(self._discards))

    # ------------------------------------------------------------------------------------------------ checks

    async def _candidate(self, args: Mapping[str, Any]) -> CandidateDraft:
        ids = self._ids(args.get("submission_ids"))
        discarded = [i for i in ids if i in self._discards]
        if discarded:
            raise _Refused("submission_ids", f"{', '.join(discarded)} were discarded in this pass; a submission is "
                           f"either cited or discarded, never both. Leave them out")
        fields = CandidateFields(text=_text(args.get("text")), happens_when=_when(args, "happens_when"),
                                 said_when=_when(args, "said_when"), basis=_basis(args.get("basis")))
        draft = _draft(args)
        relation = await self._relation(args.get("relation"), args.get("fact_id"))
        return CandidateDraft(source_ids=ids, fields=fields, draft=draft, relation=relation)

    def _ids(self, raw: Any) -> Tuple[str, ...]:
        values = [raw] if isinstance(raw, str) else raw
        if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
            raise _Refused("submission_ids", "a list of one or more ids of the new submissions in this message")
        ids = tuple(dict.fromkeys(v.strip() for v in values))
        unknown = [i for i in ids if i not in self._sent]
        if unknown:
            raise _Refused("submission_ids", f"{', '.join(unknown)} is no new submission of this message; use only the "
                           f"ids of the new submissions block")
        return ids

    async def _relation(self, raw_kind: Any, raw_fact_id: Any) -> Relation:
        kinds = ", ".join(k.value for k in RelationKind)
        try:
            kind = RelationKind(raw_kind.strip().lower()) if isinstance(raw_kind, str) else None
        except ValueError:
            kind = None
        if kind is None:
            raise _Refused("relation", f"one of {kinds}")
        if kind is RelationKind.NEW:
            return Relation(kind, None, None)
        if not isinstance(raw_fact_id, str) or not raw_fact_id.strip():
            raise _Refused("fact_id", f"relation {kind.value} needs the id of the stored fact it relates to, as "
                           f"search_existing_facts returned it; with no such fact the relation is new")
        try:
            stored = await self._facts.resolve_current(raw_fact_id.strip(), self._account_id, CONSOLIDATION_ACCEPTS)
        except FactResolutionError as exc:
            raise _Refused("fact_id", f"{exc}") from exc
        if stored is None:
            raise _Refused("fact_id", "no stored fact of the owner has this id; search again and use an id from the "
                           "results, or relation new")
        return Relation(kind, stored.fact.id, stored.fact.text)


def _text(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _Refused("text", "the candidate itself, a non-empty text")
    text = raw.strip()
    if "\n" in text or "\r" in text:
        raise _Refused("text", "one line; write the candidate as one line of full sentences")
    words = word_count(text)
    if words > CANDIDATE_MAX_WORDS:
        raise _Refused("text", f"{words} words, at most {CANDIDATE_MAX_WORDS}; it is refused, never cut. Condense it to "
                       f"one thought within {CANDIDATE_MAX_WORDS} words, or split it into candidates")
    return text


def _when(args: Mapping[str, Any], name: str) -> str:
    raw = args.get(name)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise _Refused(name, "a text, empty when it is not known")
    if "\n" in raw or "\r" in raw:
        raise _Refused(name, "one line")
    return raw.strip()


def _basis(raw: Any) -> Basis:
    basis = parse_basis_strict(raw.strip().lower() if isinstance(raw, str) else raw)
    if basis is None:
        raise _Refused("basis", "one of said, document, inferred")
    return basis


def _draft(args: Mapping[str, Any]) -> DraftClass:
    directive = args.get("directive")
    if not isinstance(directive, bool):
        raise _Refused("directive", "true or false")
    taxonomy = normalize_fact_taxonomy({k: args.get(k) for k in ("temporal_class", "context_priority")})
    temporal_class = _enum(TemporalClass, "temporal_class", taxonomy.get("temporal_class"))
    context_priority = _enum(ContextPriority, "context_priority", taxonomy.get("context_priority"))
    try:
        expiry = parse_expiry(args.get("expiry"))
    except FactExpiryError as exc:
        raise _Refused("expiry", str(exc).replace(NOTHING_WRITTEN, "").strip()) from exc
    return DraftClass(directive=directive, temporal_class=temporal_class, context_priority=context_priority,
                      expiry=_stored_expiry(expiry))


def _enum(kind: Any, name: str, raw: Any) -> str:
    try:
        value: str = parse_enum(kind, name, raw).value
        return value
    except ValueError as exc:
        raise _Refused(name, str(exc)) from exc


def _stored_expiry(expiry: Expiry) -> Dict[str, Any]:
    """The canonical form `parse_expiry` reads back to the same decision."""
    if expiry.on is not None:
        return {"kind": expiry.kind.value, "date": expiry.on.isoformat()}
    if expiry.days is not None:
        return {"kind": expiry.kind.value, "days": expiry.days}
    return {"kind": expiry.kind.value}


def _reason(raw: Any) -> DiscardReason:
    value: Optional[str] = raw.strip().lower() if isinstance(raw, str) else None
    for reason in _MODEL_REASONS:
        if value == reason.value:
            return reason
    raise _Refused("reason", f"one of {', '.join(r.value for r in _MODEL_REASONS)}")
