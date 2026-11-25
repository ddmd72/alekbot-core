"""The memory inbox's final pass as one consolidation-agent task (MCP_MEMORY_SUBMISSIONS_RFC §9): what the final pass
service sends the agent, what the agent answers, the user message and the `reported_date` span.

The task carries the confirmed candidates the service read; `encode_final_task` / `decode_final_task` move them through
the agent message's payload in process (their times stay datetimes). The result says whether the model gave its final
report with no raise (`ended`: the service then marks every sent candidate `consolidated`, §9.4) and how many fact
writes the pass made (`wrote`).

`final_message` is the mail stage's form: one code header line, then one JSON object per candidate, oldest receipt first
(code cannot sort by `said_when`, which is free text the owner may have edited, so every object carries it and the token
tells the model to rank by it, §9.1). Never the raw submission: the owner confirmed the candidate, not the raw text.

`final_span` is the pass's write span (§9.3): from the oldest receipt minus SAID_WHEN_LOOKBACK to the newest receipt. A
model `reported_date` after the newest receipt is clamped down, one far back is bounded, a missing one is the newest
receipt.

One module of the task's codecs, message and span, on purpose (a waiver of the one-public-class rule): one wire format.
"""
import json
from datetime import datetime
from typing import Any, Dict, Final, List, Mapping, Sequence, Tuple

from .local_time import local_stamp
from .memory_inbox import SAID_WHEN_LOOKBACK, Candidate

PAYLOAD_KEY: Final[str] = "inbox_final"

# The user message's first line (no placeholder).
INBOX_CANDIDATES_HEADER: Final[str] = (
    "Confirmed memory inbox candidates, one JSON object per candidate, oldest receipt first. Each one condenses what the "
    "owner told an outside assistant, or with basis inferred what that assistant concluded, and was confirmed by the "
    "owner as true: read it as a statement the owner vouches for, made at its said_when (else at its received time), and consolidate "
    "it under every rule of memory:"
)

LINE_KEYS: Final[Tuple[str, ...]] = (
    "candidate_id", "text", "happens_when", "said_when", "basis", "received", "confirmed", "edited", "draft", "relation",
)


def by_receipt(candidates: Sequence[Candidate]) -> List[Candidate]:
    """The candidates oldest receipt first (`Candidate.received`, its newest source's receipt), ties by id."""
    return sorted(candidates, key=lambda c: (c.received, c.candidate_id))


def encode_final_task(candidates: Sequence[Candidate]) -> Dict[str, Any]:
    """The candidates as the agent task's payload fields, under `PAYLOAD_KEY`, each as its id and its stored form.
    In process only (the records' times stay datetimes)."""
    return {PAYLOAD_KEY: [{"id": c.candidate_id, "record": c.to_record()} for c in candidates]}


def decode_final_task(payload: Mapping[str, Any]) -> Tuple[Candidate, ...]:
    """The candidates in an agent task's payload. Raises ValueError when there are none or one is not a candidate."""
    raw = payload.get(PAYLOAD_KEY)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"the payload carries no final task under {PAYLOAD_KEY!r}")
    out: List[Candidate] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str) \
                or not isinstance(entry.get("record"), Mapping):
            raise ValueError("an entry of the final task is not an id with its record")
        out.append(Candidate.from_record(entry["id"], entry["record"]))
    return tuple(out)


def encode_final_result(*, ended: bool, wrote: int) -> Dict[str, Any]:
    return {"ended": ended, "wrote": wrote}


def decode_final_result(result: Mapping[str, Any]) -> Tuple[bool, int]:
    """(ended, wrote) of an agent's answer. Raises ValueError for one not in `encode_final_result`'s form."""
    if not isinstance(result, Mapping):
        raise ValueError("a final result is an object")
    ended, wrote = result.get("ended"), result.get("wrote")
    if not isinstance(ended, bool) or isinstance(wrote, bool) or not isinstance(wrote, int) or wrote < 0:
        raise ValueError("a final result holds ended (a boolean) and wrote (a count)")
    return ended, wrote


def final_message(candidates: Sequence[Candidate], zone: str) -> str:
    """The pass's user message: the header line, then one object per candidate, oldest receipt first; its times are the
    owner's local stamps in `zone`."""
    lines = [INBOX_CANDIDATES_HEADER]
    for c in by_receipt(candidates):
        lines.append(json.dumps({
            "candidate_id": c.candidate_id, "text": c.text, "happens_when": c.happens_when, "said_when": c.said_when,
            "basis": c.basis.value, "received": local_stamp(c.received, zone),
            "confirmed": local_stamp(c.confirmed_at, zone) if c.confirmed_at is not None else "",
            "edited": c.edited, "draft": c.draft.to_record(), "relation": c.relation.to_record(),
        }, ensure_ascii=False))
    return "\n".join(lines)


def final_span(candidates: Sequence[Candidate]) -> Tuple[datetime, datetime]:
    """(oldest receipt − SAID_WHEN_LOOKBACK, newest receipt). Raises ValueError for no candidates."""
    if not candidates:
        raise ValueError("a final pass has candidates")
    received = [c.received for c in candidates]
    return min(received) - SAID_WHEN_LOOKBACK, max(received)
