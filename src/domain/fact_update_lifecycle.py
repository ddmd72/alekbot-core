"""The expiry, `reported_date` and trail rules of one `update_fact`, applied to the plan that `plan_fact_update` decided."""

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .entities import ContextPriority, ExpiryKind, FactEntity, FactState
from .exceptions import FactExpiryError
from .fact_expiry import EXPIRY_SHAPES, NOTHING_WRITTEN, Expiry
from .fact_lifecycle import (
    archive_fields, check_pin, compute_expiry, expiry_pinned, lifecycle_event, lifecycle_fields, required_decision,
    revival_fields, with_event,
)
from .fact_review_reasons import is_rewrite_reason, superseded_reason
from .fact_review_rules import STAYS_STALE_REFUSAL, review_action
from .fact_update import AS_HISTORY_SAME_TEXT, ParsedFactUpdate
from .fact_update_plan import FactUpdatePlan
from .fact_write_context import (
    FactWriteContext, as_utc, decide_expiry, reported_date_for_update, review_horizon, reviews_stored_memory,
)


@dataclass(frozen=True)
class FactFinish:
    """`plan` with the decision, date and trail rules applied; `notes` are lines for the tool result."""

    plan: FactUpdatePlan
    notes: Tuple[str, ...]


def finish_fact_update(
    plan: FactUpdatePlan,
    current: FactEntity,
    update: ParsedFactUpdate,
    context: FactWriteContext,
    now: datetime,
    evidence_at: Optional[datetime] = None,
    reason: Optional[str] = None,
) -> FactFinish:
    """Apply the expiry rules to `plan` (plan_fact_update's decision for `update` on `current`).

    - A version whose result is current needs the update's `expiry` (decided again from the new text); a pinned fact gets
      `none` instead. The decision is computed from the resolved `reported_date`, then must not be born due.
    - An in-place update with an `expiry` recomputes the decision the same way. One that only moves `reported_date` on a
      horizon fact moves `expires_at` to the later of the old and the new horizon.
    - A state change keeps the decision as it is and adds a `lifecycle_events` entry; a version starts with its
      predecessor's trail plus its own. The entry's reason is `reason`, else the row's `closing_reason`.

    The review and the owner (`reviews_stored_memory`) go by `review_action` instead (Facts S4 plan §3.6): a revive or a
    rewrite comes back current with a horizon counted from now and strictly not born due; an archive is
    `archive_fields`; another close is the in-place S3 write; both keep `stale_since`. `reason` is required for them
    (ValueError); `evidence_at` moves a revive's `reported_date` (the later of the two) and nothing else. Every other
    pass ignores `evidence_at`.

    A history rewrite (`update.as_history`, P2-1) is theirs alone (a timeout writes as the owner's row): any other pass
    gets ValueError. It must be a rewrite to a different text (AS_HISTORY_SAME_TEXT); its version is `archival`.

    Raises FactExpiryError, whose text is what the model reads; nothing has been written at that point.
    """
    now = as_utc(now)
    if update.as_history and not reviews_stored_memory(context):
        raise ValueError(
            f"as_history: only the review, the owner and a timeout rewrite a fact as history, not {context.actor}; "
            "nothing was written"
        )
    if reviews_stored_memory(context):
        return _finish_review(plan, current, update, context, now, evidence_at, reason)
    reported, date_note = reported_date_for_update(
        context, update.reported_date, current.reported_date, plan.kind == "version", now
    )
    notes: List[str] = [*update.notes]
    if date_note is not None:
        notes.append(date_note)
    if plan.kind == "version":
        finished, decision_note = _finish_version(plan, current, update, context, now, reported)
    else:
        finished, decision_note = _finish_in_place(plan, current, update, context, now, reported, reason)
    if decision_note is not None:
        notes.append(decision_note)
    return FactFinish(finished, tuple(notes))


def _finish_review(
    plan: FactUpdatePlan, current: FactEntity, update: ParsedFactUpdate, context: FactWriteContext, now: datetime,
    evidence_at: Optional[datetime], reason: Optional[str],
) -> FactFinish:
    if reason is None:
        raise ValueError(f"a {context.actor} write carries its code-built trail reason; none was given")
    action = review_action(current, update)
    if action is None:
        raise ValueError(STAYS_STALE_REFUSAL)
    if update.as_history and action != "rewrite":
        raise ValueError(AS_HISTORY_SAME_TEXT)
    # Only a revive rests on evidence or an answer that may move the date (plan §2.1); a rewrite or a close keeps it.
    reported, date_note = reported_date_for_update(
        context, update.reported_date, current.reported_date, action == "rewrite", now,
        evidence_at if action == "revive" else None,
    )
    notes: List[str] = [*update.notes]
    if date_note is not None:
        notes.append(date_note)
    decision_note: Optional[str] = None
    if action == "rewrite":
        finished, decision_note = _review_version(plan, current, update, context, now, reported, reason)
    elif plan.kind != "in_place":
        raise ValueError(
            f"this pass makes an {action} in place: send the state alone, without text, tags, metadata or "
            "temporal_class"
        )
    elif action == "revive":
        finished, decision_note = _review_revive(plan, current, update, context, now, reported, reason)
    elif action == "archive":
        # One rule for every review archive: `archive_and_add` writes `archive_fields` too, beside a history fact.
        finished = dataclasses.replace(plan, fields=archive_fields(current, now, context.actor, reason))
    else:
        finished, _ = _finish_in_place(plan, current, update, context, now, reported, reason)
    if decision_note is not None:
        notes.append(decision_note)
    return FactFinish(finished, tuple(notes))


def _review_revive(
    plan: FactUpdatePlan, current: FactEntity, update: ParsedFactUpdate, context: FactWriteContext, now: datetime,
    reported: datetime, reason: str,
) -> Tuple[FactUpdatePlan, Optional[str]]:
    """Back to current in place, from stale or of a current fact waiting for an answer; the entry is written either way."""
    pinned = expiry_pinned(current.domain, current.type, current.tags)
    decision = required_decision(update.expiry, pinned, _missing_for_revive)
    expiry_fields, note = review_horizon(decision, reported, now, context)
    fields = dict(plan.fields)
    fields.pop("reported_date", None)
    if reported != as_utc(current.reported_date):
        fields["reported_date"] = reported
    fields.update(revival_fields(current, expiry_fields, now, context.actor, reason))
    return dataclasses.replace(plan, fields=fields), note


def _review_version(
    plan: FactUpdatePlan, current: FactEntity, update: ParsedFactUpdate, context: FactWriteContext, now: datetime,
    reported: datetime, reason: str,
) -> Tuple[FactUpdatePlan, Optional[str]]:
    """A rewrite: a current version (whatever the stored state was), `reported_date` stored, the review horizon. Its own
    entry leads to current with the rewrite's reason (`is_rewrite_reason`, never a revive's), so the window a later
    silence doubles counts from it. A history rewrite (`as_history`) needs a text other than the stored one and is
    `archival`: out of the baseline cache, found by every search."""
    version = plan.version_fact
    if version is None:
        raise ValueError("a version plan carries its version document")
    if update.as_history and version.text == current.text:
        raise ValueError(AS_HISTORY_SAME_TEXT)
    if not is_rewrite_reason(reason):
        raise ValueError(f"a rewrite's trail reason must be a rewrite reason, not {reason!r}")
    pinned = expiry_pinned(version.domain, version.type, version.tags)
    decision = required_decision(update.expiry, pinned, lambda: _missing_for_version(update))
    expiry_fields, note = review_horizon(decision, reported, now, context)
    entry = lifecycle_event(now, None, FactState.CURRENT, context.actor, reason)
    changes: Dict[str, Any] = {
        **lifecycle_fields(FactState.CURRENT, now),
        **expiry_fields,
        "reported_date": reported,
        "stale_since": None,
        "lifecycle_events": with_event(current.lifecycle_events, entry),
    }
    if update.as_history:
        changes["context_priority"] = ContextPriority.ARCHIVAL
    return dataclasses.replace(plan, version_fact=version.model_copy(update=changes)), note


def _finish_version(
    plan: FactUpdatePlan, current: FactEntity, update: ParsedFactUpdate, context: FactWriteContext, now: datetime,
    reported: datetime,
) -> Tuple[FactUpdatePlan, Optional[str]]:
    version = plan.version_fact
    if version is None:
        raise ValueError("a version plan carries its version document")
    changes: Dict[str, Any] = {"reported_date": reported}
    note: Optional[str] = None
    if version.state is FactState.CURRENT:
        pinned = expiry_pinned(version.domain, version.type, version.tags)
        decision = required_decision(update.expiry, pinned, lambda: _missing_for_version(update))
        fields, note = decide_expiry(decision, reported, now, context)
        changes.update(fields)
    # The version's own entry has no `from`: the document is born in this state; the old one's closure is the other entry.
    entry = lifecycle_event(now, None, version.state, context.actor, f"version {version.version} of {current.id}")
    changes["lifecycle_events"] = with_event(current.lifecycle_events, entry)
    return dataclasses.replace(plan, version_fact=version.model_copy(update=changes)), note


def _finish_in_place(
    plan: FactUpdatePlan, current: FactEntity, update: ParsedFactUpdate, context: FactWriteContext, now: datetime,
    reported: datetime, reason: Optional[str],
) -> Tuple[FactUpdatePlan, Optional[str]]:
    fields = dict(plan.fields)
    fields.pop("reported_date", None)
    stored_reported = as_utc(current.reported_date)
    if reported != stored_reported:
        fields["reported_date"] = reported
    note: Optional[str] = None
    if update.state is not None and update.state != current.state:
        entry = lifecycle_event(now, current.state, update.state, context.actor, _closing_reason(update, context, reason))
        fields["lifecycle_events"] = with_event(current.lifecycle_events, entry)
    elif update.expiry is not None:
        decision = check_pin(update.expiry, expiry_pinned(current.domain, current.type, current.tags))
        decided, note = decide_expiry(decision, reported, now, context)
        fields.update(decided)
    elif reported != stored_reported:
        later = _later_horizon(current, reported)
        if later is not None:
            fields["expires_at"] = later
    return dataclasses.replace(plan, fields=fields), note


def _closing_reason(update: ParsedFactUpdate, context: FactWriteContext, reason: Optional[str]) -> str:
    """The code-built reason of an in-place state change, never model text: the caller's `reason`, else a supersede's
    successor, else the row's `closing_reason`. A row with none (the review, the owner) needs the caller's."""
    if reason is not None:
        return reason
    if update.superseded_by is not None:
        return superseded_reason(update.superseded_by)
    default = context.actor.closing_reason
    if default is None:
        raise ValueError(f"a {context.actor} state change carries its code-built trail reason; none was given")
    return default


def _later_horizon(current: FactEntity, reported: datetime) -> Optional[datetime]:
    """`expires_at` of a horizon fact whose `reported_date` moved: never earlier than it was. None = leave it."""
    if current.expiry_kind is not ExpiryKind.HORIZON or current.ttl_days is None:
        return None
    counted: datetime = compute_expiry(Expiry(ExpiryKind.HORIZON, days=current.ttl_days), reported, "UTC")["expires_at"]
    if current.expires_at is None:
        return counted
    stored = as_utc(current.expires_at)
    return counted if counted > stored else None


def _missing_for_version(update: ParsedFactUpdate) -> FactExpiryError:
    sent = [
        name
        for name, value in (
            ("text", update.content),
            ("tags", update.tags),
            ("metadata", update.metadata),
            ("temporal_class", update.temporal_class),
        )
        if value is not None
    ]
    return FactExpiryError(
        f"this update sends {', '.join(f'`{name}`' for name in sent)}, which makes a new version of the fact; "
        f"a new version needs `expiry` inside `updates`, decided again from the new text: use one of {EXPIRY_SHAPES}. "
        f"{NOTHING_WRITTEN}"
    )


def _missing_for_revive() -> FactExpiryError:
    return FactExpiryError(
        "bringing the fact back to current needs `expiry` inside `updates`, decided again with horizons counted from "
        f"now: use one of {EXPIRY_SHAPES}. {NOTHING_WRITTEN}"
    )
