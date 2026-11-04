"""The shared failure wording of the HTML render clients: every status maps to its fixed text, never to reply bytes."""
import pytest

from src.adapters.sandbox_client.sandbox_http_client import SandboxReply
from src.adapters.sandbox_client.sandbox_render_outcome import (
    SANDBOX_LOST_TEXT,
    RenderTexts,
    failure_text,
    unavailable_text,
)
from src.domain.sandbox_contract import SandboxOutcome, SandboxStatus
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable

TEXTS = RenderTexts(timeout="t-timeout", empty_output="t-empty", script_error="t-script",
                    too_large="t-large", bad_request="t-bad")


def _reply(status: SandboxStatus, detail: str = "", body: bytes = b"attacker-chosen <url>") -> SandboxReply:
    return SandboxReply(outcome=SandboxOutcome(status=status, detail=detail), body=body)


@pytest.mark.parametrize("status, expected", [
    (SandboxStatus.TIMEOUT, "t-timeout"),
    (SandboxStatus.EMPTY_OUTPUT, "t-empty"),
    (SandboxStatus.SCRIPT_ERROR, "t-script"),
    (SandboxStatus.TOO_LARGE, "t-large"),
    (SandboxStatus.BAD_REQUEST, "t-bad"),
])
def test_each_mapped_status_gets_its_kind_text(status, expected):
    assert failure_text(_reply(status, detail="render_failed"), TEXTS) == expected


def test_a_lost_sandbox_gets_the_lost_text():
    assert failure_text(_reply(SandboxStatus.ERROR, detail="sandbox_lost"), TEXTS) == SANDBOX_LOST_TEXT


def test_any_other_error_names_the_detail_code_and_never_the_body():
    text = failure_text(_reply(SandboxStatus.ERROR, detail="browser_unavailable"), TEXTS)
    assert text == "sandbox error: browser_unavailable"
    assert "attacker" not in text


def test_an_error_without_detail_names_the_status():
    assert failure_text(_reply(SandboxStatus.ERROR), TEXTS) == f"sandbox error: {SandboxStatus.ERROR.value}"


@pytest.mark.parametrize("exc", [SandboxUnavailable("down"), SandboxMisconfigured("401")])
def test_unavailable_text_names_the_kind_and_the_error_class_only(exc):
    assert unavailable_text("PDF", exc) == f"PDF rendering is unavailable ({type(exc).__name__}); try again later"
