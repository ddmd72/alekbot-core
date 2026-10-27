"""Facts S5 classify, the pure half: everything that decides, computes, serialises or renders. No I/O.

The I/O half (`fact_s5_classify.py`) scans the live collection, calls the model and writes files; this module holds the
rules both its dry run and its apply rely on, so they can be tested without a database or a model.

Imports: stdlib and `src.domain` only.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AbstractSet, Any, Dict, Final, List, Mapping, Optional, Sequence, Tuple

from src.domain.billing import calculate_cost
from src.domain.entities import ExpiryKind, FactDomain, FactEntity, FactState, FactType
from src.domain.exceptions import FactExpiryError
from src.domain.fact_expiry import EXPIRY_NONE, Expiry, expiry_refusal, parse_expiry, stored_expiry
from src.domain.fact_lifecycle import (
    compute_expiry,
    expiry_pinned,
    lifecycle_event,
    require_in_place,
    with_event,
)
from src.domain.fact_write_context import as_utc
from src.domain.local_time import local_date, local_day, local_moment, local_stamp

BATCH_SIZE: Final[int] = 25
SYSTEM_ACCOUNT: Final[str] = "SYSTEM"
TRAIL_ACTOR: Final[str] = "migration"
TAXONOMY_BLOCKS: Final[Tuple[str, ...]] = ("temporal_classes", "expiry")
REASON_MAX_CHARS: Final[int] = 200

SKIP_NO_ACCOUNT = "no_account"
SKIP_SYSTEM = "system_account"
SKIP_DECIDED = "already_decided"
SKIP_NOT_FOUND = "not_found_or_not_current"
SKIP_OWNER = "owner_unresolved"

SOURCE_MODEL = "model"
SOURCE_PINNED = "pinned"
SOURCE_UNSURE = "unsure"
SOURCE_FALLBACK = "fallback"
SOURCE_PAST = "past_at_report"
SOURCES: Final[Tuple[str, ...]] = (SOURCE_MODEL, SOURCE_PINNED, SOURCE_UNSURE, SOURCE_PAST, SOURCE_FALLBACK)
FALLBACK_MISSING = "missing"
FALLBACK_INVALID = "invalid_expiry"
FALLBACK_NO_REPORTED = "no_reported_date"
FALLBACK_CODES: Final[Tuple[str, ...]] = (FALLBACK_MISSING, FALLBACK_INVALID, FALLBACK_NO_REPORTED)

PLAN_KIND: Final[str] = "fact_s5_classify_plan"
PLAN_VERSION: Final[int] = 1

OUTCOME_APPLIED = "applied"
OUTCOME_ALREADY = "already_applied"
OUTCOME_CHANGED = "skipped_changed"
OUTCOME_FAILED = "failed"
OUTCOME_NOT_CURRENT = "not_current"   # an entry of a closed fact: apply writes current facts only
PLAN_STATES: Final[Tuple[str, ...]] = (FactState.CURRENT.value, FactState.ARCHIVED.value, FactState.INVALIDATED.value)

CHARS_PER_TOKEN: Final[float] = 3.1
OUTPUT_TOKENS_PER_FACT: Final[int] = 50
THINKING_TOKENS_PER_CALL: Final[Tuple[int, int]] = (2_000, 6_000)

_PROBLEM_UNPARSEABLE = "unparseable"
_PROBLEM_DUPLICATE = "duplicate"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_KIND_ORDER: Final[Tuple[str, ...]] = ("date", "horizon", "none")
_REPORT_COLUMNS: Final[Tuple[str, ...]] = (
    "account", "text", "domain", "type", "temporal_class", "reported", "decision", "expires_at", "due_now", "reason", "flag",
)

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "description": "One entry per fact, by its number n.",
            "items": {
                "type": "object",
                "required": ["n", "kind", "unsure", "reason"],
                "properties": {
                    "n": {"type": "integer", "description": "The fact's number as given."},
                    "kind": {"type": "string", "enum": ["date", "horizon", "none"], "description": "date, horizon or none"},
                    "date": {"type": "string", "description": "With kind date only: YYYY-MM-DD, the last day the fact holds."},
                    "days": {"type": "integer", "description": "With kind horizon only: days counted from reported_date."},
                    "unsure": {"type": "boolean", "description": "True when you are not sure; the kind is then none."},
                    "reason": {"type": "string", "description": "One short English line: the kinds_table row and the deciding words."},
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------------------------------------------------
# The scan and the selection
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScannedFact:
    fact_id: str
    account_id: str                    # "" when the document has none
    token: str                         # str(snapshot.update_time) at the scan
    text: str
    domain: Optional[str]
    type: Optional[str]
    tags: Tuple[str, ...]
    temporal_class: Optional[str]
    metadata: Mapping[str, Any]
    context: Optional[str]
    reported_date: Optional[datetime]  # aware UTC; None when absent or not a datetime
    created_at: Optional[datetime]     # same rule
    expiry_kind: Optional[str]         # the stored raw value
    state: str = "current"             # current, or archived / invalidated for a closed fact classified by id


def _lowered(value: Any) -> Optional[str]:
    """A string `domain` or `type` lowercased before the enum parse, so a stored `AGENT_DIRECTIVE` still reads as pinned.
    More facts read as pinned than under the live parse, which is the safe direction (pinned means `none`)."""
    return value.lower() if isinstance(value, str) else None


def scanned_fact(doc_id: str, data: Mapping[str, Any], update_time: Any) -> ScannedFact:
    tags = data.get("tags")
    metadata = data.get("metadata")
    context = data.get("context")
    temporal = data.get("temporal_class")
    expiry_kind = data.get("expiry_kind")
    text = data.get("text")
    account = data.get("account_id")
    state = data.get("state")
    reported = data.get("reported_date")
    created = data.get("created_at")
    return ScannedFact(
        fact_id=doc_id,
        account_id=account if isinstance(account, str) else "",
        token=str(update_time),
        text=text if isinstance(text, str) else "",
        domain=_lowered(data.get("domain")),
        type=_lowered(data.get("type")),
        tags=tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else (),
        temporal_class=temporal if isinstance(temporal, str) else None,
        metadata=metadata if isinstance(metadata, dict) else {},
        context=context if isinstance(context, str) else None,
        reported_date=as_utc(reported) if isinstance(reported, datetime) else None,
        created_at=as_utc(created) if isinstance(created, datetime) else None,
        expiry_kind=expiry_kind,
        state=state if isinstance(state, str) else "current",
    )


@dataclass(frozen=True)
class Skipped:
    fact_id: str
    reason: str


def select(
    facts: Sequence[ScannedFact], ids: Optional[AbstractSet[str]] = None
) -> Tuple[List[ScannedFact], List[Skipped]]:
    """The facts to classify and the ones left out with the reason. With `ids`, only listed facts are considered; a listed
    id that was not scanned is reported as not found."""
    selected: List[ScannedFact] = []
    skipped: List[Skipped] = []
    for fact in facts:
        if ids is not None and fact.fact_id not in ids:
            continue
        if not fact.account_id:
            skipped.append(Skipped(fact.fact_id, SKIP_NO_ACCOUNT))
        elif fact.account_id == SYSTEM_ACCOUNT:
            skipped.append(Skipped(fact.fact_id, SKIP_SYSTEM))
        elif fact.expiry_kind is not None:
            skipped.append(Skipped(fact.fact_id, SKIP_DECIDED))
        else:
            selected.append(fact)
    if ids is not None:
        scanned = {f.fact_id for f in facts}
        skipped.extend(Skipped(fact_id, SKIP_NOT_FOUND) for fact_id in sorted(ids - scanned))
    return selected, skipped


def _enum_or_none(enum_type: Any, value: Optional[str]) -> Any:
    try:
        return enum_type(value) if value is not None else None
    except ValueError:
        return None


def is_pinned(fact: ScannedFact) -> bool:
    """Directives, principles and mindset facts take `none` only (the domain's own rule, not restated here)."""
    return expiry_pinned(_enum_or_none(FactDomain, fact.domain), _enum_or_none(FactType, fact.type), fact.tags)


def batches(facts: Sequence[ScannedFact], size: int = BATCH_SIZE) -> List[List[ScannedFact]]:
    return [list(facts[i:i + size]) for i in range(0, len(facts), size)]


# ---------------------------------------------------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------------------------------------------------


def _local_day_or_none(moment: Optional[datetime], timezone_name: str) -> Optional[str]:
    return local_day(moment, timezone_name) if moment is not None else None


def fact_line(n: int, fact: ScannedFact, timezone_name: str) -> Dict[str, Any]:
    """One fact as the model sees it: no fact id, no account id, no stored expiry."""
    return {
        "n": n,
        "text": fact.text,
        "domain": fact.domain,
        "temporal_class": fact.temporal_class,
        "tags": list(fact.tags),
        "metadata": dict(fact.metadata),
        "context": fact.context,
        "reported_date": _local_day_or_none(fact.reported_date, timezone_name),
        "created_at": _local_day_or_none(fact.created_at, timezone_name),
    }


def user_message(lines: Sequence[Mapping[str, Any]], problems: Optional[Mapping[int, str]] = None) -> str:
    out: List[str] = []
    if problems:
        listed = ", ".join(f"n={n} ({problems[n]})" for n in sorted(problems))
        out.append(f"Your previous answer gave no usable decision for these facts: {listed}. Decide each of them again.")
    out.append("Stored facts to classify, one JSON object per line:")
    out.extend(json.dumps(line, ensure_ascii=False, default=str, sort_keys=True) for line in lines)
    return "\n".join(out)


def _block_end(lines: Sequence[str], start: int, name: str) -> int:
    """The index of the line holding the brace that closes the block opened on `lines[start]`."""
    depth = 0
    opened = False
    for index in range(start, len(lines)):
        in_string = False
        escaped = False
        line = lines[index]
        position = 0
        while position < len(line):
            char = line[position]
            position += 1
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "/" and line[position:position + 1] == "/":
                break  # a comment: the rest of the line holds no braces
            elif char == "{":
                depth += 1
                opened = True
            elif char == "}":
                depth -= 1
        if opened and depth == 0:
            return index
        if depth < 0:
            break
    raise ValueError(f"block {name!r} has unbalanced braces")


def extract_blocks(token_content: str, names: Sequence[str] = TAXONOMY_BLOCKS) -> str:
    """The named blocks of a token's text, verbatim (the `//` comment line right above each included), joined by a blank line."""
    lines = token_content.split("\n")
    pieces: List[str] = []
    for name in names:
        starts = [i for i, line in enumerate(lines) if line.strip().startswith((f"{name}: {{", f"{name} {{"))]
        if len(starts) != 1:
            raise ValueError(f"block {name!r} must occur exactly once, found {len(starts)}")
        start = starts[0]
        end = _block_end(lines, start, name)
        first = start - 1 if start > 0 and lines[start - 1].strip().startswith("//") else start
        pieces.append("\n".join(lines[first:end + 1]))
    return "\n\n".join(pieces)


def system_prompt(taxonomy_blocks: str, instruction: str) -> str:
    if not instruction.strip():
        raise ValueError("the classify instruction is empty")
    return taxonomy_blocks.rstrip() + "\n\n" + instruction.strip() + "\n"


# ---------------------------------------------------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RawDecision:
    kind: Any
    date: Any
    days: Any
    unsure: bool
    reason: str


def _reason_text(value: Any) -> str:
    return ("" if value is None else str(value)).strip()[:REASON_MAX_CHARS]


def read_answer(raw: Optional[str], numbers: AbstractSet[int]) -> Tuple[Dict[int, RawDecision], Dict[int, str]]:
    """(decisions by n, problems by n). Every number asked ends up in exactly one of the two."""
    try:
        parsed = json.loads(raw) if raw is not None else None
    except (TypeError, ValueError):
        parsed = None
    entries = parsed.get("decisions") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        return {}, {n: _PROBLEM_UNPARSEABLE for n in numbers}
    decisions: Dict[int, RawDecision] = {}
    duplicated: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        n = entry.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n not in numbers:
            continue
        if n in decisions or n in duplicated:
            decisions.pop(n, None)
            duplicated.add(n)
            continue
        decisions[n] = RawDecision(
            kind=entry.get("kind"),
            date=entry.get("date"),
            days=entry.get("days"),
            unsure=entry.get("unsure") is True,
            reason=_reason_text(entry.get("reason")),
        )
    problems = {n: _PROBLEM_DUPLICATE for n in duplicated}
    problems.update({n: FALLBACK_MISSING for n in numbers if n not in decisions and n not in duplicated})
    return decisions, problems


@dataclass(frozen=True)
class Decision:
    fact_id: str
    source: str
    expiry: Expiry
    code: Optional[str]
    model_kind: Optional[str]
    reason: str


def judge(
    fact: ScannedFact, raw: Optional[RawDecision], timezone_name: str
) -> Tuple[Optional[Decision], Optional[str]]:
    """(decision, None) or (None, why it is unusable). A date already past by `now` is a correct decision (due), but a date
    before the local day (in `timezone_name`) of the fact's `reported_date` was past when the owner said it: a past event,
    `none`, source `past_at_report`."""
    if raw is None:
        return None, FALLBACK_MISSING
    if raw.unsure:
        return Decision(fact.fact_id, SOURCE_UNSURE, EXPIRY_NONE, None, str(raw.kind), raw.reason), None
    try:
        expiry = parse_expiry({"kind": raw.kind, "date": raw.date, "days": raw.days})
    except FactExpiryError:
        return None, FALLBACK_INVALID
    if expiry.kind is ExpiryKind.HORIZON and fact.reported_date is None:
        return None, FALLBACK_NO_REPORTED
    if expiry.kind is ExpiryKind.DATE and expiry.on is not None and fact.reported_date is not None:
        if expiry.on < local_date(fact.reported_date, timezone_name):
            said = f"date {expiry.on.isoformat()}"
            return Decision(fact.fact_id, SOURCE_PAST, EXPIRY_NONE, None, said, raw.reason), None
    return Decision(fact.fact_id, SOURCE_MODEL, expiry, None, expiry.kind.value, raw.reason), None


def pinned_decision(fact: ScannedFact) -> Decision:
    return Decision(fact.fact_id, SOURCE_PINNED, EXPIRY_NONE, None, None, "")


def fallback_decision(fact: ScannedFact, code: str) -> Decision:
    return Decision(fact.fact_id, SOURCE_FALLBACK, EXPIRY_NONE, code, None, "")


def planned_fields(decision: Decision, fact: ScannedFact, timezone_name: str) -> Dict[str, Any]:
    """`expiry_kind`, `ttl_days`, `expires_at` for a decision. `reported_date` is read only for a horizon, which `judge`
    guarantees has one; the epoch stands in for the others."""
    return compute_expiry(decision.expiry, fact.reported_date or _EPOCH, timezone_name)


def trail_reason(source: str, code: Optional[str]) -> str:
    detail = f"{source}: {code}" if source == SOURCE_FALLBACK and code else source
    return f"expiry classified at S5 ({detail})"


# ---------------------------------------------------------------------------------------------------------------------
# The plan file
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanEntry:
    fact_id: str
    account_id: str
    token: str
    fields: Dict[str, Any]
    source: str
    code: Optional[str]
    state: str = "current"


def plan_entry(decision: Decision, fact: ScannedFact, fields: Mapping[str, Any]) -> PlanEntry:
    return PlanEntry(
        fact_id=fact.fact_id,
        account_id=fact.account_id,
        token=fact.token,
        fields={
            "expiry_kind": ExpiryKind(fields["expiry_kind"]),
            "ttl_days": fields["ttl_days"],
            "expires_at": fields["expires_at"],
        },
        source=decision.source,
        code=decision.code,
        state=fact.state,
    )


def _serialised_fields(fields: Mapping[str, Any]) -> Dict[str, Any]:
    expires_at = fields["expires_at"]
    return {
        "expiry_kind": ExpiryKind(fields["expiry_kind"]).value,
        "ttl_days": fields["ttl_days"],
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
    }


def plan_document(
    entries: Sequence[PlanEntry],
    skipped: Sequence[Skipped],
    not_decided: Sequence[str],
    *,
    collection: str,
    models: Mapping[str, str],
    generated_at: datetime,
    taxonomy_sha256: str,
    instruction_sha256: str,
    stopped: Optional[str],
) -> Dict[str, Any]:
    return {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "generated_at": generated_at.isoformat(),
        "collection": collection,
        "models": dict(models),
        "prompt": {"taxonomy_sha256": taxonomy_sha256, "instruction_sha256": instruction_sha256},
        "stopped": stopped,
        "decisions": [
            {
                "fact_id": e.fact_id,
                "account_id": e.account_id,
                "token": e.token,
                "fields": _serialised_fields(e.fields),
                "source": e.source,
                "code": e.code,
                "state": e.state,
            }
            for e in entries
        ],
        "skipped": [{"fact_id": s.fact_id, "reason": s.reason} for s in skipped],
        "not_decided": list(not_decided),
    }


class PlanFileError(ValueError):
    """The plan file is not one this script wrote, or is damaged; nothing is applied."""


def _refuse(index: int, why: str) -> PlanFileError:
    return PlanFileError(f"plan entry {index}: {why}")


def _read_entry(index: int, item: Any) -> PlanEntry:
    if not isinstance(item, dict):
        raise _refuse(index, "not an object")
    ids: Dict[str, str] = {}
    for name in ("fact_id", "account_id", "token"):
        value = item.get(name)
        if not isinstance(value, str) or not value:
            raise _refuse(index, f"{name} is missing")
        ids[name] = value
    source = item.get("source")
    if source not in SOURCES:
        raise _refuse(index, "unknown source")
    code = item.get("code")
    if code is not None and code not in FALLBACK_CODES:
        raise _refuse(index, "unknown code")
    state = item.get("state", FactState.CURRENT.value)
    if state not in PLAN_STATES:
        raise _refuse(index, "unknown state")
    raw = item.get("fields")
    if not isinstance(raw, dict):
        raise _refuse(index, "fields are missing")
    try:
        kind = ExpiryKind(raw.get("expiry_kind"))
    except ValueError:
        raise _refuse(index, "unknown expiry_kind") from None
    ttl_days = raw.get("ttl_days")
    if ttl_days is not None and (not isinstance(ttl_days, int) or isinstance(ttl_days, bool)):
        raise _refuse(index, "ttl_days is not a whole number")
    expires_text = raw.get("expires_at")
    expires_at: Optional[datetime] = None
    if expires_text is not None:
        try:
            expires_at = datetime.fromisoformat(expires_text)
        except (TypeError, ValueError):
            raise _refuse(index, "expires_at is not a datetime") from None
        if expires_at.tzinfo is None:
            raise _refuse(index, "expires_at is not timezone-aware")
    expiry = stored_expiry(kind, ttl_days, expires_at, "UTC")
    if expiry is None:
        raise _refuse(index, "the kind lacks its data")
    refusal = expiry_refusal(expiry)
    if refusal is not None:
        raise _refuse(index, refusal)
    # The shape compute_expiry writes: a date has no ttl_days, a horizon has both, none has neither.
    shape_ok = {
        ExpiryKind.DATE: ttl_days is None and expires_at is not None,
        ExpiryKind.HORIZON: ttl_days is not None and expires_at is not None,
        ExpiryKind.NONE: ttl_days is None and expires_at is None,
    }[kind]
    if not shape_ok:
        raise _refuse(index, "the fields are not the shape of their kind")
    return PlanEntry(
        ids["fact_id"], ids["account_id"], ids["token"],
        {"expiry_kind": kind, "ttl_days": ttl_days, "expires_at": expires_at}, source, code, state,
    )


def read_plan(doc: Mapping[str, Any], collection: str) -> List[PlanEntry]:
    """The plan's entries, after the whole document is validated. Raises PlanFileError naming the first bad entry."""
    if not isinstance(doc, Mapping):
        raise PlanFileError("the plan is not an object")
    if doc.get("kind") != PLAN_KIND:
        raise PlanFileError("not a classify plan")
    if doc.get("version") != PLAN_VERSION:
        raise PlanFileError("unsupported plan version")
    if doc.get("collection") != collection:
        raise PlanFileError("the plan is for another collection")
    items = doc.get("decisions")
    if not isinstance(items, list):
        raise PlanFileError("the plan has no decisions list")
    entries: List[PlanEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        entry = _read_entry(index, item)
        if entry.fact_id in seen:
            raise _refuse(index, "duplicate fact_id")
        seen.add(entry.fact_id)
        entries.append(entry)
    return entries


def apply_fields(entry: PlanEntry, stored_events: Sequence[Mapping[str, Any]], now: datetime) -> Dict[str, Any]:
    """The in-place write of one entry: the three expiry fields and one trail entry."""
    event = lifecycle_event(now, FactState.CURRENT, FactState.CURRENT, TRAIL_ACTOR, trail_reason(entry.source, entry.code))
    fields: Dict[str, Any] = {
        "expiry_kind": entry.fields["expiry_kind"],
        "ttl_days": entry.fields["ttl_days"],
        "expires_at": entry.fields["expires_at"],
        "lifecycle_events": with_event(stored_events, event),
    }
    require_in_place(fields)
    return fields


def _utc_or_none(moment: Optional[datetime]) -> Optional[datetime]:
    return local_moment(moment, "UTC") if moment is not None else None


def already_applied(entry: PlanEntry, stored: FactEntity) -> bool:
    """True when the stored fact carries the entry's fields and its last trail entry is this script's, for this entry."""
    if stored.expiry_kind != entry.fields["expiry_kind"] or stored.ttl_days != entry.fields["ttl_days"]:
        return False
    if _utc_or_none(stored.expires_at) != _utc_or_none(entry.fields["expires_at"]):
        return False
    if not stored.lifecycle_events:
        return False
    last = stored.lifecycle_events[-1]
    return last.get("actor") == TRAIL_ACTOR and last.get("reason") == trail_reason(entry.source, entry.code)


# ---------------------------------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------------------------------


def _decision_text(expiry: Expiry) -> str:
    if expiry.kind is ExpiryKind.DATE and expiry.on is not None:
        return f"date {expiry.on.isoformat()}"
    if expiry.kind is ExpiryKind.HORIZON:
        return f"horizon {expiry.days} d"
    return "none"


def _flag(decision: Decision) -> str:
    if decision.source == SOURCE_MODEL:
        return ""
    if decision.source == SOURCE_UNSURE:
        return f"unsure (model said {decision.model_kind})"
    if decision.source == SOURCE_PAST:
        return f"past at report (model said {decision.model_kind})"
    if decision.source == SOURCE_FALLBACK:
        return f"fallback: {decision.code}"
    return decision.source


def report_row(
    fact: ScannedFact, decision: Decision, fields: Mapping[str, Any], timezone_name: str, now: datetime
) -> Dict[str, Any]:
    expires_at = fields["expires_at"]
    return {
        "account": fact.account_id[:8],
        "text": fact.text,
        "domain": fact.domain,
        "type": fact.type,
        "temporal_class": fact.temporal_class,
        "reported": local_day(fact.reported_date, timezone_name) if fact.reported_date is not None else "",
        "decision": _decision_text(decision.expiry),
        "expires_at": local_stamp(expires_at, timezone_name) if expires_at is not None else "",
        "due_now": expires_at is not None and expires_at <= now,
        "reason": decision.reason,
        "flag": _flag(decision),
    }


def _cell(value: Any) -> str:
    if isinstance(value, bool):
        value = "yes" if value else "no"
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _row_order(row: Mapping[str, Any]) -> Tuple[int, int, str]:
    kind = str(row["decision"]).split(" ", 1)[0]
    rank = _KIND_ORDER.index(kind) if kind in _KIND_ORDER else len(_KIND_ORDER)
    return (0 if row["due_now"] else 1, rank, str(row["text"]))


def render_report(rows: Sequence[Mapping[str, Any]], summary_lines: Sequence[str]) -> str:
    out = ["# Facts S5 classify — dry run", ""]
    out.extend(f"- {line}" for line in summary_lines)
    out.append("")
    out.append("| " + " | ".join(_REPORT_COLUMNS) + " |")
    out.append("|" + "|".join(" --- " for _ in _REPORT_COLUMNS) + "|")
    for row in sorted(rows, key=_row_order):
        out.append("| " + " | ".join(_cell(row[column]) for column in _REPORT_COLUMNS) + " |")
    return "\n".join(out) + "\n"


def _count(values: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def summary(
    decisions: Sequence[Decision],
    fields_by_id: Mapping[str, Mapping[str, Any]],
    skipped: Sequence[Skipped],
    not_decided: Sequence[str],
    now: datetime,
    spend: Optional[Mapping[str, float]] = None,
    zones: Optional[Mapping[str, str]] = None,
    no_reported_date: int = 0,
) -> Dict[str, Any]:
    by_kind = {kind: 0 for kind in _KIND_ORDER}
    by_source = {source: 0 for source in SOURCES}
    fallback_by_code: Dict[str, int] = {}
    due_now = 0
    for decision in decisions:
        by_kind[decision.expiry.kind.value] += 1
        by_source[decision.source] += 1
        if decision.source == SOURCE_FALLBACK and decision.code:
            fallback_by_code[decision.code] = fallback_by_code.get(decision.code, 0) + 1
        expires_at = fields_by_id[decision.fact_id]["expires_at"]
        if expires_at is not None and expires_at <= now:
            due_now += 1
    return {
        "decided": len(decisions),
        "by_kind": by_kind,
        "by_source": by_source,
        "fallback_by_code": fallback_by_code,
        "due_now": due_now,
        "skipped": _count([s.reason for s in skipped]),
        "not_decided": len(not_decided),
        "no_reported_date": no_reported_date,
        "zones": dict(zones or {}),
        "cost": dict(spend or {}),
    }


def _pairs(values: Mapping[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in values.items()) if values else "none"


def summary_lines(summary: Mapping[str, Any]) -> List[str]:
    """`name: value` lines: counts, account prefixes with their zone, spend. No fact id, no text."""
    lines = [
        f"decided: {summary['decided']}",
        f"by_kind: {_pairs(summary['by_kind'])}",
        f"by_source: {_pairs(summary['by_source'])}",
        f"fallback_by_code: {_pairs(summary['fallback_by_code'])}",
        f"due_now: {summary['due_now']}",
        f"skipped: {_pairs(summary['skipped'])}",
        f"not_decided: {summary['not_decided']}",
        f"no_reported_date: {summary['no_reported_date']}",
    ]
    lines.extend(f"zone {prefix}: {zone}" for prefix, zone in summary["zones"].items())
    lines.extend(f"cost {name}: ${value:.4f}" for name, value in summary["cost"].items())
    return lines


def estimate_usd(model: str, calls: Sequence[Tuple[int, int]]) -> Optional[Tuple[float, float]]:
    """(low, high) USD for calls given as (input chars, facts); None when the model is unpriced."""
    low = 0.0
    high = 0.0
    for chars, facts in calls:
        input_tokens = int(chars / CHARS_PER_TOKEN)
        base = facts * OUTPUT_TOKENS_PER_FACT
        call_low = calculate_cost(model, input_tokens, base + THINKING_TOKENS_PER_CALL[0])
        call_high = calculate_cost(model, input_tokens, base + THINKING_TOKENS_PER_CALL[1])
        if call_low == 0.0 or call_high == 0.0:
            return None
        low += call_low
        high += call_high
    return low, high
