"""`redact_traceback` over its 4 000-character cap keeps the end, where the exception's type and message are printed
(and the first line), never only the outermost frames."""
from src.domain.log_redaction import redact_traceback

TOKEN = "eyJhbGciOiJIUzI1NiIsImtpZCI6InZvaWNlLW1jcCJ9.eyJzdWIiOiJ1c3ItMSJ9.c2lnbmF0dXJlLXRhaWwtdGVzdA"


def _deep(depth: int) -> None:
    if depth == 0:
        raise RuntimeError(f"socket closed by the provider, last frame carried {TOKEN}")
    _deep(depth - 1)


def _long_traceback_exception() -> RuntimeError:
    try:
        _deep(120)
    except RuntimeError as exc:
        return exc
    raise AssertionError("not raised")


def test_a_long_traceback_keeps_its_final_exception_line_and_its_first_line():
    out = redact_traceback(_long_traceback_exception())

    assert len(out) <= 4000
    assert out.startswith("Traceback (most recent call last):")
    assert out.rstrip().endswith("RuntimeError: socket closed by the provider, last frame carried <redacted>")
    assert TOKEN not in out and "c2lnbmF0dXJlLXRhaWwtdGVzdA" not in out


def test_a_short_traceback_is_kept_whole():
    try:
        raise ValueError("bad descriptor")
    except ValueError as exc:
        out = redact_traceback(exc)

    assert out.startswith("Traceback (most recent call last):") and out.rstrip().endswith("ValueError: bad descriptor")
    assert "…" not in out
