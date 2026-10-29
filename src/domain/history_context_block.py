"""The text one `*_context` key adds to a model message's full_text (ConversationHandler._prepare_history_texts). Pure.

The drive's key renders as one readable line per file (drive_history.py); every other key as its compact JSON block.
The evals that seed history (scripts/validation/drive_routing_probe.py, drive_eval/specialist_ab.py) call the same
function, so their seeds stay in the shape production stores.
"""
from __future__ import annotations

import json
from typing import Any

from .drive_history import DRIVE_CONTEXT_KEY, drive_history_lines


def history_context_block(key: str, value: Any) -> str:
    """The block for one metadata key; "" when the key is not a `*_context` key or there is nothing to render."""
    if not key.endswith("_context") or not value:
        return ""
    if key == DRIVE_CONTEXT_KEY:
        return "\n".join(drive_history_lines(value))
    return json.dumps({key: value}, ensure_ascii=False, separators=(",", ":"))
