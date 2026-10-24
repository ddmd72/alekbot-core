"""The expiry, `reported_date` and trail rules of one `update_fact`, applied to the plan that `plan_fact_update` decided."""

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .entities import ExpiryKind, FactEntity, FactState
from .exceptions import FactExpiryError
from .fact_expiry import EXPIRY_SHAPES, NOTHING_WRITTEN, Expiry, as_expiry_kind
from .fact_lifecycle import check_pin, compute_expiry, expiry_pinned, lifecycle_event, required_decision, with_event
from .fact_update import ParsedFactUpdate
from .fact_update_plan import FactUpdatePlan
from .fact_write_context import FactWriteContext, as_utc, decide_expiry, reported_date_for_update


@dataclass(frozen=True)
class FactFinish:
    """`plan` with the decision, date and trail rules applied; `notes` are lines for the tool result."""

    plan: FactUpdatePlan
    notes: Tuple[str, ...]


def finish_fact_update(
    plan: FactUpdatePlan, current: FactEntity, update: ParsedFactUpdate, context: FactWriteContext, now: datetime
) -> FactFinish:
    """Apply the expiry rules to `plan` (plan_fact_update's decision for `update` on `current`).

    - A version whose result is current needs the update's `expiry` (decided again from the new text); a pinned fact gets
      `none` instead. The decision is computed from the resolved `reported_date`, then must not be born due.
    - An in-place update with an `expiry` recomputes the decision the same way. One that only moves `reported_date` on a
      horizon fact moves `expires_at` to the later of the old and the new horizon.
    - A state change keeps the decision as it is and adds a `lifecycle_events` entry; a version starts with its
      predecessor's trail plus its own.

    Raises FactExpiryError, whose text is what the model reads; nothing has been written at that point.
    """
    now = as_utc(now)
    reported, date_note = reported_date_for_update(
        context, update.reported_date, current.reported_date, plan.kind == "version", now
    )
    notes: List[str] = [*update.notes]
    if date_note is not None:
        notes.append(date_note)
    if plan.kind == "version":
        finished, decision_note = _finish_version(plan, current, update, context, now, reported)
    else:
        finished, decision_note = _finish_in_place(plan, current, update, context, now, reported)
    if decision_note is not None:
        notes.append(decision_note)
    return FactFinish(finished, tuple(notes))


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
    reported: datetime,
) -> Tuple[FactUpdatePlan, Optional[str]]:
    fields = dict(plan.fields)
    fields.pop("reported_date", None)
    stored_reported = as_utc(current.reported_date)
    if reported != stored_reported:
        fields["reported_date"] = reported
    note: Optional[str] = None
    if update.state is not None and update.state != current.state:
        reason = _closing_reason(update, context)
        entry = lifecycle_event(now, current.state, update.state, context.actor, reason)
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


def _closing_reason(update: ParsedFactUpdate, context: FactWriteContext) -> str:
    """The code-built reason of an in-place state change: never model text."""
    if update.superseded_by is not None:
        return f"superseded by {update.superseded_by}"
    return "directive cap" if context.actor == "backstop" else "model"


def _later_horizon(current: FactEntity, reported: datetime) -> Optional[datetime]:
    """`expires_at` of a horizon fact whose `reported_date` moved: never earlier than it was. None = leave it."""
    if as_expiry_kind(current.expiry_kind) is not ExpiryKind.HORIZON or current.ttl_days is None:
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
