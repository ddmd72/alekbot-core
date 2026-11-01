"""Line matchers for the workspace `grep` command (AGENT_WORKSPACE_RFC §7.4).

Literal mode uses plain `str` search (no regex engine at all). Regex mode uses RE2 (`google-re2`): it runs in time
linear in the input, so a regular expression supplied by a model cannot stall the 1-vCPU service. The price is the RE2
syntax: no backreferences and no lookaround.
"""

from typing import Callable

import re2

LineMatcher = Callable[[str], bool]

RE2_SYNTAX_NOTE = "RE2 syntax: no backreferences or lookaround"


class InvalidPattern(ValueError):
    """The pattern cannot be compiled; the message names the engine's complaint and the fix."""


def _error_text(e: Exception) -> str:
    raw = e.args[0] if e.args else str(e)
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def compile_line_matcher(pattern: str, *, regex: bool = False, ignore_case: bool = False) -> LineMatcher:
    """A function telling whether one line contains the pattern. Raises InvalidPattern for a bad regex."""
    if not regex:
        if ignore_case:
            needle = pattern.casefold()
            return lambda line: needle in line.casefold()
        return lambda line: pattern in line
    try:
        compiled = re2.compile(("(?i)" if ignore_case else "") + pattern)
    except re2.error as e:
        raise InvalidPattern(f"invalid regex ({_error_text(e)}); {RE2_SYNTAX_NOTE}") from e
    return lambda line: compiled.search(line) is not None
