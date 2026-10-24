"""The words of the fact tool result: the dict `FactManagementPort` returns for a write.

The adapter writes them, the consolidation agent reads the outcome, the tracker watches the writes, the directive-cap
backstop checks one status, and the dry-run kit counts them. One spelling lives here, so a misspelt literal cannot turn
a write into "not a write". The words are `domain.fact_tool_result`; the port's docstrings point here.
"""

from typing import Any, Dict, FrozenSet, List, Sequence

from .consolidation_pass_stats import (
    OUTCOME_BORN_DUE, OUTCOME_ERROR, OUTCOME_EXPIRY, OUTCOME_OK, OUTCOME_REVIEW_REFUSAL,
)
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
_REVIEW_REFUSALS: FrozenSet[str] = frozenset({REFUSED_BASIS, REFUSED_SCOPE, REFUSED_ASK})

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


def outcome_of(result: Any) -> str:
    """What a tool result came to, as one of the `OUTCOME_*` values of `consolidation_pass_stats`: an expiry refusal
    (born due or not), a review refusal (`basis`, `scope`, `ask`), a failed or missing fact or an `error` key, else ok.
    A result that is not a dict is ok."""
    if not isinstance(result, dict):
        return OUTCOME_OK
    refused = result.get("refused")
    if refused == REFUSED_EXPIRY:
        return OUTCOME_BORN_DUE if result.get("born_due") else OUTCOME_EXPIRY
    if refused in _REVIEW_REFUSALS:
        return OUTCOME_REVIEW_REFUSAL
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
