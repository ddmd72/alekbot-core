"""
Email gate — the closed keep/drop reasons of the email classifier and the code rule over its answer (email index v2 §4,
slice 9b). Pure, mypy --strict.

The gate reads one listed page of new mail (headers and a snippet per mail) and answers, per mail, keep or drop with one
reason. A kept mail costs one card extraction and becomes searchable and a memory candidate; a dropped one is findable
only while recent or by a literal identifier. So the code leans to keep: whatever the answer does not settle cleanly is
kept.

**The code rule** (`gate_decisions`): a listed mail the answer leaves out, a reason outside both lists, or a reason that
belongs to the other decision (a drop with a keep reason, or the reverse) is kept and marked `forced`; an id not on the
page is ignored; when one id comes twice, keep wins.

Public interface:
    KEEP_REASONS, DROP_REASONS      value -> meaning, in the order the block renders them
    GateDecision                    keep, reason (a known value, else None), forced
    render_gate_reasons_block() -> str
    gate_decisions(items, page_ids) -> Dict[str, GateDecision]   one decision per page id
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence

DECISION_KEEP = "keep"
DECISION_DROP = "drop"

KEEP_REASONS: Mapping[str, str] = MappingProxyType({
    "record": "a transaction, an order or its delivery progress, a booking, a ticket, a bill or payment request, a "
              "statement, a policy, a contract or an official decision concerning the owner or the household",
    "correspondence": "a person writing to the owner, or a reply in a conversation of the owner's",
    "notice": "a school, health provider, employer, authority, landlord or service provider writing about the owner's "
              "own matter or account, other than signing in to it or activity on a platform",
    "unsure": "the headers and the start of the text do not settle it",
})

DROP_REASONS: Mapping[str, str] = MappingProxyType({
    "promotion": "advertising: an offer, a sale, a discount or a recommendation to buy",
    "newsletter": "a newsletter, digest or general announcement written for many readers",
    "social": "platform activity: reactions, follows, connection requests, activity digests",
    "sign_in": "a sign-in code, a sign-in alert, a password reset or an address verification",
    "automated": "a machine notice with nothing of the owner's own matter in it",
})

_KEEP_HEADING = "KEEP REASONS (decision keep):"
_DROP_HEADING = "DROP REASONS (decision drop):"


@dataclass(frozen=True)
class GateDecision:
    keep: bool
    reason: Optional[str]
    forced: bool


def render_gate_reasons_block() -> str:
    """The reasons as the gate's prompt carries them: one `- value: meaning` line per value under each heading."""
    keep = [f"- {value}: {meaning}" for value, meaning in KEEP_REASONS.items()]
    drop = [f"- {value}: {meaning}" for value, meaning in DROP_REASONS.items()]
    return "\n".join([_KEEP_HEADING, *keep, _DROP_HEADING, *drop])


def gate_decisions(items: Sequence[Mapping[str, Any]], page_ids: Sequence[str]) -> Dict[str, GateDecision]:
    """One decision per id of `page_ids` from the model's answer `items` (`{email_id, decision, reason}` objects), by
    the code rule of the module docstring. A missing mail is kept with no reason."""
    on_page = set(page_ids)
    answered: Dict[str, GateDecision] = {}
    for item in items:
        email_id = item.get("email_id")
        if not isinstance(email_id, str) or email_id not in on_page:
            continue
        decision = _decide(item.get("decision"), item.get("reason"))
        earlier = answered.get(email_id)
        if earlier is None or (decision.keep and not earlier.keep):
            answered[email_id] = decision
    return {email_id: answered.get(email_id, GateDecision(keep=True, reason=None, forced=True))
            for email_id in page_ids}


def _decide(decision: Any, reason: Any) -> GateDecision:
    known = reason if isinstance(reason, str) and (reason in KEEP_REASONS or reason in DROP_REASONS) else None
    if decision == DECISION_DROP and known in DROP_REASONS:
        return GateDecision(keep=False, reason=known, forced=False)
    if decision == DECISION_KEEP and known in KEEP_REASONS:
        return GateDecision(keep=True, reason=known, forced=False)
    return GateDecision(keep=True, reason=known, forced=True)
