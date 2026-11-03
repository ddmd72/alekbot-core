"""Marker texts for the drive connector's tests: every key of the connector contract, each text naming its own key, so
a test asserts which line was chosen and in which order, never a sentence of the real tokens (their wording is tuned
live, D4).

- `marker(key)`: the marker text of a key (`<R_FIND>`); the formatted keys carry their placeholders
  (`<STATUS {status}>`, `<WHAT_TO_DO {line}>`, `<ROUTE {ref}>`, the page line's six).
- `marker_bodies(**overrides)`: the two token bodies (`### key` sections); `overrides` replaces a key's text.
- `marker_texts(**overrides)`: the parsed `ConnectorTexts` of those bodies.
- `TokenStore`: an in-memory stand-in for `TokenRepository.get` holding the two tokens (risk level `safe`), whose
  bodies a test can replace (a token upload).
"""
from __future__ import annotations

from typing import Dict, Tuple

from src.domain import drive_connector_contract as c
from src.domain.connector_texts import ConnectorTexts
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId

TOOL_TOKEN = "MCP_DRIVE_TOOL_TEXTS"
RESULT_TOKEN = "MCP_DRIVE_RESULT_TEXTS"

_FORMATTED = {
    c.STATUS: "<STATUS {status}>",
    c.WHAT_TO_DO: "<WHAT_TO_DO {line}>",
    c.ROUTE: "<ROUTE {ref}>",
    c.PAGE_NEXT: "<PAGE_NEXT {first}-{last} of {total} {path} v{version} next {next}>",
}


def marker(key: str) -> str:
    return _FORMATTED.get(key, f"<{key}>")


def _body(keys, overrides: Dict[str, str]) -> str:
    return "\n".join(f"### {key}\n{overrides.get(key, marker(key))}\n" for key in sorted(keys))


def marker_bodies(**overrides: str) -> Tuple[str, str]:
    return _body(c.REQUIRED_TOOL_KEYS, overrides), _body(c.REQUIRED_RESULT_KEYS, overrides)


def marker_texts(**overrides: str) -> ConnectorTexts:
    tool_body, result_body = marker_bodies(**overrides)
    return ConnectorTexts.from_bodies(tool_body, result_body, c.REQUIRED_TOOL_KEYS, c.REQUIRED_RESULT_KEYS)


def _token(token_id: str, content: str) -> Token:
    return Token(id=TokenId(token_id), category=TokenCategory("mcp_drive"), class_=TokenClass("mcp_texts"),
                 content=content, metadata={"validation": {"risk_level": "safe"}})


class TokenStore:
    """`get(token_id)` as `TokenRepository` answers it; KeyError for a token not stored."""

    def __init__(self, **overrides: str) -> None:
        self.tokens: Dict[str, Token] = {}
        self.upload(**overrides)

    def upload(self, **overrides: str) -> None:
        tool_body, result_body = marker_bodies(**overrides)
        self.tokens = {TOOL_TOKEN: _token(TOOL_TOKEN, tool_body), RESULT_TOKEN: _token(RESULT_TOKEN, result_body)}

    async def get(self, token_id: TokenId) -> Token:
        return self.tokens[str(token_id)]
