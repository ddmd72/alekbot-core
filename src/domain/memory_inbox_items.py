"""The memory inbox tool's liberal input (MCP_MEMORY_SUBMISSIONS_RFC §5.2; `decisions/mcp_tool_schema_liberal_input.md`):
whatever a client sent as `items` becomes normalised drafts, never an error. Pure: stdlib only, no I/O.

Shapes taken: a list of objects (the advertised one); one object; a bare string (one item with only `information`); a
list of strings (that many items); a JSON-encoded array or object in a string; explicit nulls (empty). A field that is
not text is empty (a number is its digits); an unknown field is ignored.

Per item: `information` trimmed, an empty one drops the item (`dropped_empty`); `basis` read leniently (trimmed, any
case) as `said` / `document` / `inferred`, anything else `inferred`, the least trusted; `said_when` and `happens_when`
trimmed. Caps cut, never fail: usable items past MAX_ITEMS_PER_CALL are dropped (`over_call`; an empty item does not
count against the cap), and a field over its char cap (MAX_INFORMATION_CHARS, MAX_WHEN_CHARS) is cut there and named in
the draft's `cut` (`cut_fields` counts them over the call).
"""
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .memory_inbox import MAX_INFORMATION_CHARS, MAX_ITEMS_PER_CALL, MAX_WHEN_CHARS, Basis, SubmissionDraft


@dataclass(frozen=True)
class NormalizedItems:
    """The drafts of one call, in the order sent, and what was left out: `dropped_empty` items with no information,
    `over_call` usable items past MAX_ITEMS_PER_CALL, `cut_fields` fields cut at their cap."""

    drafts: Tuple[SubmissionDraft, ...]
    dropped_empty: int
    over_call: int
    cut_fields: int


_CAPS: Mapping[str, int] = {"information": MAX_INFORMATION_CHARS, "said_when": MAX_WHEN_CHARS,
                            "happens_when": MAX_WHEN_CHARS}


def normalize_items(raw: Any) -> NormalizedItems:
    """`raw` as the call's drafts; never raises."""
    drafts: List[SubmissionDraft] = []
    dropped_empty = over_call = cut_fields = 0
    for element in _elements(raw):
        draft = _draft(element)
        if draft is None:
            dropped_empty += 1
        elif len(drafts) >= MAX_ITEMS_PER_CALL:
            over_call += 1
        else:
            drafts.append(draft)
            cut_fields += len(draft.cut)
    return NormalizedItems(drafts=tuple(drafts), dropped_empty=dropped_empty, over_call=over_call,
                           cut_fields=cut_fields)


def _elements(raw: Any) -> List[Any]:
    """The items `raw` holds: a list's elements, a JSON array or object in a string parsed, anything else one item."""
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped[:1] in ("[", "{"):
            try:
                raw = json.loads(stripped)
            except ValueError:
                return [raw]
        else:
            return [raw]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]


def _draft(element: Any) -> Optional[SubmissionDraft]:
    """One element as a draft, or None when it carries no information."""
    fields: Dict[str, Any] = dict(element) if isinstance(element, Mapping) else {"information": element}
    texts = {name: _text(fields.get(name)) for name in _CAPS}
    if not texts["information"]:
        return None
    cut = frozenset(name for name, cap in _CAPS.items() if len(texts[name]) > cap)
    texts = {name: text[:_CAPS[name]] for name, text in texts.items()}
    return SubmissionDraft(information=texts["information"], basis=_basis(fields.get("basis")),
                           said_when=texts["said_when"], happens_when=texts["happens_when"], cut=cut)


def _text(value: Any) -> str:
    """A field's text, trimmed: a string as sent, a number as its digits, anything else (null, a list, a map, a
    boolean) empty."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _basis(value: Any) -> Basis:
    """The lenient reading: one of the three values in any case and spacing, else `inferred`."""
    if isinstance(value, str):
        wanted = value.strip().casefold()
        for basis in Basis:
            if wanted == basis.value:
                return basis
    return Basis.INFERRED
