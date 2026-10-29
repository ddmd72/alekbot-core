"""Errands from a phone call: `send_to_chat` (VOICE_COMPANION_RFC §4.15.2, revised 2026-10-29).

`send_to_chat` is a top-level tool of the realtime session, next to `delegate_to_specialist`: something
the caller wants in their chat. Alek finds or makes it and posts it there; Lelik does not hear back.
Internally it is one more delegation, the errand intent of the same name (ASYNC, through the Alek gateway),
so the main service, Smart's side and the logs use one word. The relay treats it differently from a
question: no dispatch filler, no waiting notes, no abandon at call end.

The names live here because services/ may not import infrastructure/ (the manifest reads them from here
too, so they have one source).
"""
from typing import Any, Dict, Mapping, Optional

# The tool the session declares and the internal intent it runs as: one vocabulary.
SEND_TO_CHAT = "send_to_chat"
SEND_TO_CHAT_INTENT = SEND_TO_CHAT
# The tool's one argument.
SEND_TO_CHAT_REQUEST = "request"


def delegation_arguments(tool_name: Optional[str], arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """The `delegate_to_specialist` arguments a session tool call runs as. `send_to_chat(request)` becomes the
    errand intent with the request as its query; any other tool's arguments pass through unchanged."""
    if tool_name == SEND_TO_CHAT:
        return {"intent": SEND_TO_CHAT_INTENT, "query": str(arguments.get(SEND_TO_CHAT_REQUEST) or "")}
    return dict(arguments)


def is_errand(arguments: Mapping[str, Any]) -> bool:
    return arguments.get("intent") == SEND_TO_CHAT_INTENT
