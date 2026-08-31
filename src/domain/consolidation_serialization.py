"""
Shared message-serialization for consolidation-style background batches.

Extracted from what was duplicated inline in two call sites (main.py's
FirestoreSessionStore overflow_callback, ConversationHandler's $consolidate
command) — this is the third call site (CompanionExtractionBatch's write
path, RFC docs/10_rfcs/COMPANION_AGENTS_RFC.md §5: "how a model turn is
serialized into the batch — consolidation_text's summary-vs-full choice,
per channel rather than hardcoded"), which is the reason for extracting
rather than copying a third time. ConversationHandler's own $consolidate
copy is NOT migrated by this change — a second duplicate remains there,
a known, separately-tracked cleanup.
"""
from typing import Dict, List

from .companion_config import CompanionTextMode
from .llm import Message


def serialize_messages_for_consolidation(
    messages: List[Message],
    text_mode: CompanionTextMode = CompanionTextMode.SUMMARY,
) -> List[Dict]:
    """Serialize a message batch for a consolidation-style background pipeline.

    Model parts: SUMMARY mode uses `text` (the summary); FULL mode prefers
    `full_text`, falling back to `text` when absent. User parts always prefer
    `consolidation_text` (an explicit fact-save annotation) over `text`,
    independent of `text_mode` — there is no FULL/SUMMARY distinction for
    user-authored text in this codebase.
    """
    serialized: List[Dict] = []
    for msg in messages:
        if msg.role == "model":
            if text_mode == CompanionTextMode.FULL:
                parts = [{"text": p.full_text or p.text} for p in msg.parts if (p.full_text or p.text)]
            else:
                parts = [{"text": p.text} for p in msg.parts if p.text]
        else:
            parts = [{"text": p.consolidation_text or p.text} for p in msg.parts if (p.consolidation_text or p.text)]
        serialized.append({
            "role": msg.role,
            "parts": parts,
            "created_at": msg.created_at,
        })
    return serialized
