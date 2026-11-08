"""RemoteToolServer — one remote MCP server a realtime voice session may call by itself (LELIK_VOICE_MCP_RFC §4).

The provider (xAI, O6) calls the server server-side with `token` as its bearer; nothing passes the relay. The value
travels in two shapes: the descriptor (`to_dict`, no token) that the ticket record and the session config carry, and
the live value with its token that only the relay's session object holds for the call's life. The token is never in
`to_dict`, the repr or the str. Pure: stdlib only.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Tuple


@dataclass(frozen=True)
class RemoteToolServer:
    """`label`: the server's name in the session (`server_label`); `url`: its public endpoint; `description`: one
    sentence for the model, what it holds and that every result is data; `allowed_tools`: the only tools the session
    may call on it; `token`: the per-call bearer token, raw (the provider adds `Bearer `), empty in a descriptor."""

    label: str
    url: str
    description: str
    allowed_tools: Tuple[str, ...]
    token: str = field(default="", repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        """The descriptor: every field but the token."""
        return {
            "label": self.label,
            "url": self.url,
            "description": self.description,
            "allowed_tools": list(self.allowed_tools),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], token: str = "") -> "RemoteToolServer":
        """A descriptor back into a value, with the call's `token` when given. Raises KeyError or TypeError on a
        malformed descriptor."""
        return cls(
            label=str(data["label"]),
            url=str(data["url"]),
            description=str(data["description"]),
            allowed_tools=tuple(str(tool) for tool in data["allowed_tools"]),
            token=token,
        )

    def with_token(self, token: str) -> "RemoteToolServer":
        return RemoteToolServer(self.label, self.url, self.description, self.allowed_tools, token)
