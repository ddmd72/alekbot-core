"""The words of the fact tool result: the dict `FactManagementPort` returns for a write.

The adapter writes them (an update's dict through `update_result`, from its typed `FactUpdateOutcome`), the consolidation
agent reads the outcome, the tracker watches the writes, the directive-cap
backstop checks one status, and the dry-run kit counts them. One spelling lives here, so a misspelt literal cannot turn
a write into "not a write". The words are `domain.fact_tool_result`; the port's docstrings point here.
"""

from typing import Any, Dict, FrozenSet, List, Sequence

from .consolidation_pass_stats import (
    OUTCOME_BORN_DUE, OUTCOME_ERROR, OUTCOME_EXPIRY, OUTCOME_OK, OUTCOME_REVIEW_REFUSAL, OUTCOME_SCOPE_REFUSAL,
)
from .fact_update_outcome import EXPIRY_REFUSALS, FactUpdateOutcome, UpdateRefusal
from .fact_write_context import NO_READABLE_SOURCE_NOTE

# `status` of a tool result.
STATUS_CREATED = "created"
STATUS_UPDATED = "updated"
STATUS_MERGED = "merged"
STATUS_FAILED = "failed"
STATUS_NOT_FOUND = "not_found"
# An `ask_owner` the review pass accepted and holds (nothing is written by the call).
STATUS_ASKED = "asked"

# Key of an `update_fact` result that archived its fact beside a new history fact: the id of that new fact.
HISTORY_FACT_ID = "history_fact_id"

# The statuses that mean a fact was written.
WRITTEN_STATUSES: FrozenSet[str] = frozenset({STATUS_CREATED, STATUS_UPDATED, STATUS_MERGED})

# Value of the `refused` tag. A new tag is declared together with the write that sets it.
REFUSED_EXPIRY = "expiry"
# The review pass's refusals: the reason behind a change (`basis`), the pass's own rules and the update's arguments (batch,
# one result per fact, state; `scope`), an ask (`ask`).
REFUSED_BASIS = "basis"
REFUSED_SCOPE = "scope"
REFUSED_ASK = "ask"
_REVIEW_OUTCOMES: Dict[str, str] = {
    REFUSED_BASIS: OUTCOME_REVIEW_REFUSAL, REFUSED_ASK: OUTCOME_REVIEW_REFUSAL, REFUSED_SCOPE: OUTCOME_SCOPE_REFUSAL,
}

# Codes of the notes a reader needs to recognise. The text stays in the domain beside the rule that writes it; the
# result carries the code in `note_codes`, so no reader matches prose.
NOTE_NO_READABLE_SOURCE = "no_readable_source"

_NOTE_CODES: Dict[str, str] = {
    NO_READABLE_SOURCE_NOTE: NOTE_NO_READABLE_SOURCE,
}


def expiry_refusal_tags(born_due: bool) -> Dict[str, Any]:
    """The tags of an expiry refusal: `refused`, and `born_due` when the expiry was well formed but already due."""
    return {"refused": REFUSED_EXPIRY, "born_due": True} if born_due else {"refused": REFUSED_EXPIRY}


def refused_result(fact_id: Any, tag: str, message: str) -> Dict[str, Any]:
    """A failed call with nothing written: `refused` names the rule (`tag`), `message` is the text the model reads."""
    return {"fact_id": fact_id, "status": STATUS_FAILED, "refused": tag, "message": message}


def update_result(outcome: FactUpdateOutcome) -> Dict[str, Any]:
    """The model's `update_fact` result for a typed update outcome: the one renderer of an update's dict.

    Written: `fact_id` (the document written), `status` updated, `version`, `message`, then `history_fact_id`, the notes
    (`with_notes`) and `resolved_from`, in that order. Not written: `fact_id` (the id asked for), `status` (not_found
    or failed), the expiry tags of an expiry refusal, and the refusal text as `message`."""
    if outcome.refusal is not None:
        status = STATUS_NOT_FOUND if outcome.refusal is UpdateRefusal.NOT_FOUND else STATUS_FAILED
        tags = expiry_refusal_tags(outcome.refusal is UpdateRefusal.BORN_DUE) if outcome.refusal in EXPIRY_REFUSALS else {}
        return {"fact_id": outcome.fact_id, "status": status, **tags, "message": outcome.refusal_text}
    message = (
        "Fact updated successfully" if outcome.history_fact_id is None
        else f"Fact archived; history fact {outcome.history_fact_id} created beside it"
    )
    if outcome.resolved_from is not None:
        message = (
            f"{message}; fact {outcome.fact_id} was replaced earlier, "
            f"so the update was applied to its current version {outcome.current_id}"
        )
    result: Dict[str, Any] = {
        "fact_id": outcome.written_id, "status": STATUS_UPDATED, "version": outcome.version, "message": message,
    }
    if outcome.history_fact_id is not None:
        result[HISTORY_FACT_ID] = outcome.history_fact_id
    with_notes(result, outcome.notes)
    if outcome.resolved_from is not None:
        result["resolved_from"] = outcome.resolved_from
    return result


def outcome_of(result: Any) -> str:
    """What a tool result came to, as one of the `OUTCOME_*` values of `consolidation_pass_stats`: an expiry refusal
    (born due or not), a review refusal (`basis`, `ask`), a scope refusal (`scope`), a failed or missing fact or an
    `error` key, else ok. It reads the model's dict (the tool results a pass's stats count); code reads a typed update's
    `FactUpdateOutcome` itself.
    A result that is not a dict is ok."""
    if not isinstance(result, dict):
        return OUTCOME_OK
    refused = result.get("refused")
    if refused == REFUSED_EXPIRY:
        return OUTCOME_BORN_DUE if result.get("born_due") else OUTCOME_EXPIRY
    if isinstance(refused, str) and refused in _REVIEW_OUTCOMES:
        return _REVIEW_OUTCOMES[refused]
    if result.get("status") in (STATUS_FAILED, STATUS_NOT_FOUND) or "error" in result:
        return OUTCOME_ERROR
    return OUTCOME_OK


def note_codes_of(notes: Sequence[str]) -> List[str]:
    """The codes of the known notes among `notes`, in order, matched by equality."""
    return [_NOTE_CODES[note] for note in notes if note in _NOTE_CODES]


def with_notes(result: Dict[str, Any], notes: Sequence[str]) -> Dict[str, Any]:
    """Put the notes into the tool result (in place, and returned): `note` joins them with "; ", and `note_codes`
    lists the codes of the known ones, present only when there is at least one. Each element of `notes` is one whole
    note, so a code is found by equality; never pass several notes glued into one string."""
    if notes:
        result["note"] = "; ".join(notes)
        codes = note_codes_of(notes)
        if codes:
            result["note_codes"] = codes
    return result
