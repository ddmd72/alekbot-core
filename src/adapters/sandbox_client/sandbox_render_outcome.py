"""
Shared outcome mapping of the HTML render clients (`render.pdf`, `render.png`): a failed sandbox reply becomes the
text of the port's own error. Reply bodies are never decoded (the failure text is fixed here), and the systemic
`SandboxUnavailable` / `SandboxMisconfigured` are folded into the port error by `unavailable_text`.
"""
from __future__ import annotations

from dataclasses import dataclass

from ...domain.sandbox_contract import SandboxStatus
from .sandbox_http_client import SandboxReply

SANDBOX_LOST_TEXT = "sandbox_lost: the run ended without a result twice (the sandbox instance was lost)"


@dataclass(frozen=True)
class RenderTexts:
    """The failure wording of one render kind."""
    timeout: str
    empty_output: str
    script_error: str
    too_large: str
    bad_request: str


def failure_text(reply: SandboxReply, texts: RenderTexts) -> str:
    status = reply.outcome.status
    if status == SandboxStatus.ERROR and reply.outcome.detail == "sandbox_lost":
        return SANDBOX_LOST_TEXT
    mapped = {
        SandboxStatus.TIMEOUT: texts.timeout,
        SandboxStatus.EMPTY_OUTPUT: texts.empty_output,
        SandboxStatus.SCRIPT_ERROR: texts.script_error,
        SandboxStatus.TOO_LARGE: texts.too_large,
        SandboxStatus.BAD_REQUEST: texts.bad_request,
    }.get(status)
    return mapped or f"sandbox error: {reply.outcome.detail or status.value}"


def unavailable_text(kind: str, exc: Exception) -> str:
    return f"{kind} rendering is unavailable ({type(exc).__name__}); try again later"
