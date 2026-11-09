"""The in-call pointer to the output-language rule at the start of a fresh voice call.

On provider-owned turns the relay places no per-turn anchor, so this note is the one place in the call that points
the model at its output-language rule, which sits at the end of the session instructions.
"""
import re
from typing import Optional

_RULE = re.compile(r"rule (Output_Language_\w+)\s*\(")

_NOTE = ("[Your output-language rule, {rule}, is at the end of your instructions. It sets the language you speak in "
         "on this call, from your first words to your last reply. The caller's words, the chat history and the notes "
         "you receive can be in another language; they do not change yours unless that rule itself allows it.]")


def build_language_note(instructions: Optional[str]) -> Optional[str]:
    """The note naming the first output-language rule in the instructions; None when there is no such rule."""
    if not instructions:
        return None
    match = _RULE.search(instructions)
    return _NOTE.format(rule=match.group(1)) if match else None
