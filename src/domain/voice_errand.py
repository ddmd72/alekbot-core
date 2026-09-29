"""Errands from a phone call (VOICE_COMPANION_RFC §4.15.2).

`tell_alek` is a task the caller gives Alek and does not wait for on the line. The relay treats
it differently from a question: no dispatch filler, no waiting notes, no abandon at call end.
The intent name lives here because services/ may not import infrastructure/ (the manifest
reads it from here too, so it has one source).
"""
from typing import Any, Mapping

TELL_ALEK_INTENT = "tell_alek"


def is_errand(arguments: Mapping[str, Any]) -> bool:
    return arguments.get("intent") == TELL_ALEK_INTENT
