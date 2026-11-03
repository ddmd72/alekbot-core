"""The drive connector's result for one tool call, composed for an outside client (DRIVE_MCP_CONNECTOR_RFC §5.5, §6).

    Drive result: <status>              STATUS, from the outcome class
    What to do: <line>                  WHAT_TO_DO, the one line the status was decided by (below)
    Reading: <line>                     zero or more: `reading_keys` on this one outcome, texts from the token
    <block>                             the call's own text; passages re-rendered with the token's PAGE_NEXT
    <language line>                     LANGUAGE, after a block that shows passages

"What to do", by the first rule that holds:
- the call did not finish in time (`NOT_FINISHED`): UNFINISHED, no block;
- the open withheld the file (`DriveOutcome.withheld`): the `W_<code>` line; the block is the file's label;
- the open was refused as too long (`facts.too_long`): ROUTE with the file's ref; the block is the refusal text;
- any other refusal (`REFUSED`) but the index's "the bot cannot open this file" (`facts.inaccessible`), and an item
  that does not exist (`NOT_FOUND` on any call but a search or a passages result, whose text is the result): status
  `refused`, REFUSED_GUIDANCE; the block is the REFUSED line. One neutral line for "outside", "not a drive ref" and
  "no such item", so a result is never an existence oracle; the chat's refusal lines never reach the connector (§7.7);
- content (`OK`), or a search or passages call that found nothing (its text is the result): ANSWER_FROM_RESULTS; the
  block is the call's text;
- every other outcome: its own text (a correctable request error, not connected, gone, a failure), no block.

No "Specialist note", no `drive_context`, no delivery items: the client reads the result itself. `cap_result` is the
hard size guard every tool's result passes (§6.1): cut at a line, then the OVER_SIZE line. Pure: no I/O.

Public interface:
    compose_connector_result(outcome, texts, *, nonce) -> str
    cap_result(text, over_size, limit=RESULT_MAX_CHARS) -> str
    connector_status(outcome) -> DriveStatus         the status the result's first line prints
    RESULT_MAX_CHARS                                 100,000: the guard, and the size a client is told to accept
"""
from __future__ import annotations

import secrets
from typing import Callable, Dict, Mapping, Tuple

from . import drive_answer_texts as t
from . import drive_connector_contract as c
from .connector_texts import ConnectorTexts
from .drive_answer import reading_keys, reading_texts
from .drive_collected_result import CollectedResult
from .drive_excerpt_render import render_excerpts
from .drive_op import DriveOp
from .drive_outcome import DriveOutcome
from .drive_outcome_class import OutcomeClass
from .drive_status import DriveStatus

# The hard guard on every tool's result; also the `maxResultSizeChars` an open declares (§6.1).
RESULT_MAX_CHARS = 100_000

_STATUS: Dict[OutcomeClass, DriveStatus] = {
    OutcomeClass.OK: DriveStatus.OK,
    # a request-shape error is a refusal of the request, as in the chat's answer (DRIVE_SPECIALIST_RFC §7.2)
    OutcomeClass.REQUEST_ERROR: DriveStatus.REFUSED,
    OutcomeClass.NOT_CONNECTED: DriveStatus.NOT_CONNECTED,
    OutcomeClass.INDEXING_OFF: DriveStatus.INDEXING_OFF,
    OutcomeClass.REBUILDING: DriveStatus.REBUILDING,
    OutcomeClass.NOT_FOUND: DriveStatus.NOT_FOUND,
    OutcomeClass.REFUSED: DriveStatus.REFUSED,
    OutcomeClass.FAILED: DriveStatus.FAILED,
    OutcomeClass.NOT_POSSIBLE: DriveStatus.NOT_POSSIBLE,
    OutcomeClass.STILL_RUNNING: DriveStatus.STILL_RUNNING,
    OutcomeClass.NOT_FINISHED: DriveStatus.UNFINISHED,
}

# The result kind each read's block is collected under: `reading_keys` reads it for a search and an open.
_KIND: Dict[DriveOp, str] = {
    DriveOp.SEARCH: t.KIND_SEARCH, DriveOp.LIST: t.KIND_LISTING, DriveOp.OPEN: t.KIND_OPEN,
    DriveOp.PASSAGES: t.KIND_PASSAGES, DriveOp.NOTE_READ: t.KIND_NOTE,
}
# Reads whose text is their result even when nothing was found (cards with a coverage line, a passages state).
_RESULT_TEXT_OPS = frozenset({DriveOp.SEARCH, DriveOp.PASSAGES})


def _default_nonce() -> str:
    return secrets.token_hex(8)


def connector_status(outcome: DriveOutcome) -> DriveStatus:
    """The status of one call's result: its class's, as the chat's answer maps it; `refused` for every outcome the
    neutral refusal answers (a missing item included: its status must not tell it apart either)."""
    return DriveStatus.REFUSED if _neutral_refusal(outcome) else _STATUS[outcome.cls]


def _neutral_refusal(outcome: DriveOutcome) -> bool:
    """A refusal or a missing item, answered by the one neutral REFUSED line (the module docstring's rule)."""
    if outcome.withheld is not None or outcome.facts.too_long:
        return False
    if outcome.cls is OutcomeClass.REFUSED:
        return not outcome.facts.inaccessible
    return (outcome.cls is OutcomeClass.NOT_FOUND and outcome.op is not DriveOp.SEARCH
            and outcome.excerpts is None)


def compose_connector_result(outcome: DriveOutcome, texts: ConnectorTexts, *,
                             nonce: Callable[[], str] = _default_nonce) -> str:
    """One call's result text (the module docstring's shape and rules). `nonce` gives each passages file its wrapper's
    nonce when a passages result is re-rendered. Raises KeyError when `texts` lacks a key: never a silent drop."""
    r = texts.results
    line, block = _line_and_block(outcome, r, nonce)
    out = [r[c.STATUS].format(status=connector_status(outcome).value), r[c.WHAT_TO_DO].format(line=line)]
    blocks = [CollectedResult(handle="r1", step=1, kind=_KIND.get(outcome.op, outcome.op.value), outcome=outcome)]
    out.extend(t.READING_PREFIX + text for text in reading_texts(reading_keys(blocks), r))
    if block:
        out.append(block)
    if _shows_passages(outcome):
        out.append(r[c.LANGUAGE])
    return "\n".join(out)


def cap_result(text: str, over_size: str, limit: int = RESULT_MAX_CHARS) -> str:
    """`text` when it fits `limit`; otherwise its head cut at the last line break that leaves room, then `over_size`
    on its own line, `limit` characters at most in all. A head with no line break in reach is cut mid-line."""
    if len(text) <= limit:
        return text
    room = limit - len(over_size) - 1
    if room <= 0:
        return over_size[:limit]
    cut = text.rfind("\n", 0, room + 1)
    head = text[:cut] if cut > 0 else text[:room]
    return f"{head}\n{over_size}"


def _line_and_block(outcome: DriveOutcome, r: Mapping[str, str], nonce: Callable[[], str]) -> Tuple[str, str]:
    if outcome.cls is OutcomeClass.NOT_FINISHED:
        return r[c.UNFINISHED], ""
    if outcome.withheld is not None:
        label = outcome.label_of(outcome.items[0]) if outcome.items else ""
        return r[c.text_key_of_withheld(outcome.withheld)], label
    if outcome.facts.too_long:
        ref = outcome.ref_of(outcome.items[0]) if outcome.items else ""
        return r[c.ROUTE].format(ref=ref), outcome.text
    if _neutral_refusal(outcome):
        return r[c.REFUSED_GUIDANCE], r[c.REFUSED]
    if outcome.cls is OutcomeClass.OK or (outcome.cls is OutcomeClass.NOT_FOUND and outcome.op in _RESULT_TEXT_OPS):
        return r[c.ANSWER_FROM_RESULTS], _block_text(outcome, r, nonce)
    return _block_text(outcome, r, nonce), ""


def _block_text(outcome: DriveOutcome, r: Mapping[str, str], nonce: Callable[[], str]) -> str:
    """The call's own text; a passages result re-rendered with the connector's next-page line (E10)."""
    if outcome.excerpts is None:
        return outcome.text
    return render_excerpts(outcome.excerpts, nonce=nonce, page_next=r[c.PAGE_NEXT]).text


def _shows_passages(outcome: DriveOutcome) -> bool:
    excerpts = outcome.excerpts
    return excerpts is not None and any(f.chunks for f in excerpts.files)
