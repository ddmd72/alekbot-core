"""The memory inbox tool's results (MCP_MEMORY_SUBMISSIONS_RFC §5.2, §5.3): one call's outcome as one text.

Every word comes from the result texts (`MCP_MEMORY_INBOX_RESULT_TEXTS`, keys in `memory_inbox_mcp_contract`); this
module only picks the lines and fills their counts. The one text it does not take from the tokens is the account
lock's, `ACCOUNT_LOCKED_TOOL_ERROR`, which every MCP resource answers alike (the caller sends it).

A success is `RECEIVED` with the items stored, then `CUT` with the items dropped past the call's cap plus the fields
cut at their char cap (when any), then `OVER_DAY` with the items not stored because the day's cap was reached (when
any). None of them is an error. `is_error` is MCP's "the call did not do its operation": no usable item, a store
failure, or a grant no longer allowed (`REFUSED`, the tasks app's neutral refusal). No line ever carries submitted text. `status` names the answer for the span and the log line only. Pure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping

from . import memory_inbox_mcp_contract as contract
from .memory_inbox import AddReceipt

OK = "ok"
NOTHING_USABLE = "nothing_usable"
STORE_FAILED = "store_failed"
GRANT_REFUSED = "grant_refused"


@dataclass(frozen=True)
class InboxToolAnswer:
    text: str
    is_error: bool
    status: str


def received(results: Mapping[str, str], receipt: AddReceipt, cut: int) -> InboxToolAnswer:
    """A stored call: how many items were received, then how many were cut and how many were over the day's cap."""
    lines: List[str] = [results[contract.RECEIVED].format(received=receipt.stored)]
    if cut:
        lines.append(results[contract.CUT].format(cut=cut))
    if receipt.over_day:
        lines.append(results[contract.OVER_DAY].format(over_day=receipt.over_day))
    return InboxToolAnswer(text="\n".join(lines), is_error=False, status=OK)


def nothing_usable(results: Mapping[str, str]) -> InboxToolAnswer:
    """The one input error: no item carried information."""
    return InboxToolAnswer(text=results[contract.NOTHING_USABLE], is_error=True, status=NOTHING_USABLE)


def store_failed(results: Mapping[str, str]) -> InboxToolAnswer:
    return InboxToolAnswer(text=results[contract.STORE_FAILED], is_error=True, status=STORE_FAILED)


def refused(results: Mapping[str, str]) -> InboxToolAnswer:
    """The token's grant is no longer allowed: nothing was stored."""
    return InboxToolAnswer(text=results[contract.REFUSED], is_error=True, status=GRANT_REFUSED)
