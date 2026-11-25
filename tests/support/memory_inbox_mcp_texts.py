"""Marker texts for the memory inbox MCP resource's tests: every key of `memory_inbox_mcp_contract`, each text naming
its own key and carrying exactly its placeholders, so a test asserts which line was chosen, in which order and with
which counts, never a sentence of the real tokens (their wording is tuned live).

- `marker(key)`: the marker text of a key (`<NOTHING_USABLE>`, `<RECEIVED {received}>`);
- `InboxTokenStore`: an in-memory stand-in for `TokenRepository.get` holding the two tokens (risk level `safe`); a test
  can drop one (a token never uploaded).
"""
from __future__ import annotations

from typing import Dict

from src.domain import memory_inbox_mcp_contract as c
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId

TOOL_TOKEN = "MCP_MEMORY_INBOX_TOOL_TEXTS"
RESULT_TOKEN = "MCP_MEMORY_INBOX_RESULT_TEXTS"


def marker(key: str) -> str:
    names = sorted(c.RESULT_PLACEHOLDERS.get(key, frozenset()))
    return "<" + " ".join([key, *("{" + name + "}" for name in names)]) + ">"


def _body(keys) -> str:
    return "\n".join(f"### {key}\n{marker(key)}\n" for key in sorted(keys))


def _token(token_id: str, content: str) -> Token:
    return Token(id=TokenId(token_id), category=TokenCategory("mcp_memory_inbox"), class_=TokenClass("mcp_texts"),
                 content=content, metadata={"validation": {"risk_level": "safe"}})


class InboxTokenStore:
    """`get(token_id)` as `TokenRepository` answers it; KeyError for a token not stored."""

    def __init__(self) -> None:
        self.tokens: Dict[str, Token] = {TOOL_TOKEN: _token(TOOL_TOKEN, _body(c.REQUIRED_TOOL_KEYS)),
                                         RESULT_TOKEN: _token(RESULT_TOKEN, _body(c.REQUIRED_RESULT_KEYS))}

    async def get(self, token_id: TokenId) -> Token:
        return self.tokens[str(token_id)]
