"""The drive connector's texts: a token body of keyed sections, parsed and checked (drive MCP connector concept §4.2,
§4.3).

A body is `### <key>` header lines, each followed by its text:

    ### search_drive
    Search the owner's drive.
    Two lines are kept as two lines.
    ### search_drive.query
    The words to look for.

A text is the lines up to the next header, without its leading and trailing blank lines; inner newlines are kept.
Keys are free strings (`search_drive`, `search_drive.query`, `R_FIND`). Pure: no I/O; which keys a surface requires
is the caller's (the service that loads the tokens).

Public interface:
    parse_keyed_sections(body) -> dict[str, str]                 raises ValueError on a malformed body
    ConnectorTexts.from_bodies(tool_body, result_body, required_tool_keys, required_result_keys) -> ConnectorTexts
    MAX_TOOL_DESCRIPTION_CHARS                                   the bound on every tool text
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import AbstractSet, Dict, List, Mapping

# The longest tool or parameter description a client is known to take whole (Claude Code; concept §4.3).
MAX_TOOL_DESCRIPTION_CHARS = 4096

_HEADER = "###"


def _header_key(line: str) -> str | None:
    """The key of a header line (`###`, then whitespace or the line's end), or None for a text line. `####` is text."""
    if not line.startswith(_HEADER):
        return None
    rest = line[len(_HEADER):]
    if rest and not rest[0].isspace():
        return None
    return rest.strip()


def _section_text(lines: List[str]) -> str:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def parse_keyed_sections(body: str) -> dict[str, str]:
    """Each `### <key>` header's text, in body order. Raises ValueError naming the offending key or line for: text
    before the first header that is not blank, an empty key, a duplicate key, an empty text."""
    sections: Dict[str, List[str]] = {}
    current: str | None = None
    for number, line in enumerate(body.splitlines(), start=1):
        key = _header_key(line)
        if key is None:
            if current is None:
                if line.strip():
                    raise ValueError(f"text before the first '### <key>' header, line {number}: {line!r}")
                continue
            sections[current].append(line)
            continue
        if not key:
            raise ValueError(f"empty key in the header on line {number}: {line!r}")
        if key in sections:
            raise ValueError(f"duplicate key {key!r} on line {number}")
        sections[key] = []
        current = key
    texts = {key: _section_text(lines) for key, lines in sections.items()}
    for key, text in texts.items():
        if not text:
            raise ValueError(f"empty text for key {key!r}")
    return texts


def _key_problems(name: str, found: AbstractSet[str], required: AbstractSet[str]) -> List[str]:
    problems: List[str] = []
    missing = sorted(required - found)
    unknown = sorted(found - required)
    if missing:
        problems.append(f"{name}: missing keys {', '.join(missing)}")
    if unknown:
        problems.append(f"{name}: unknown keys {', '.join(unknown)}")
    return problems


@dataclass(frozen=True)
class ConnectorTexts:
    """The connector's tool texts (server instructions, tool and parameter descriptions) and result texts (status
    lines, reading lines by key, …), each a read-only key → text mapping."""
    tools: Mapping[str, str]
    results: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        object.__setattr__(self, "results", MappingProxyType(dict(self.results)))

    @classmethod
    def from_bodies(cls, tool_body: str, result_body: str, required_tool_keys: AbstractSet[str],
                    required_result_keys: AbstractSet[str]) -> ConnectorTexts:
        """Parse both bodies; each key set must EQUAL its required set. Raises ValueError naming every missing and
        every unknown key of both bodies at once, then every tool text over `MAX_TOOL_DESCRIPTION_CHARS`."""
        tools = parse_keyed_sections(tool_body)
        results = parse_keyed_sections(result_body)
        problems = (_key_problems("tool texts", set(tools), set(required_tool_keys))
                    + _key_problems("result texts", set(results), set(required_result_keys)))
        if problems:
            raise ValueError("; ".join(problems))
        too_long = [f"{key} ({len(text)} chars)" for key, text in tools.items()
                    if len(text) > MAX_TOOL_DESCRIPTION_CHARS]
        if too_long:
            raise ValueError(f"tool texts over {MAX_TOOL_DESCRIPTION_CHARS} characters: {', '.join(too_long)}")
        return cls(tools=tools, results=results)
