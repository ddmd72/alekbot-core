"""`redact_for_log`: provider and exception text made safe for a log line (LELIK_VOICE_MCP_RFC §5.4)."""
import pytest

from src.domain.log_redaction import redact_for_log

TOKEN = "eyJhbGciOiJIUzI1NiIsImtpZCI6InZvaWNlLW1jcCJ9.eyJzdWIiOiJ1c3ItMSJ9.c2lnbmF0dXJl"


@pytest.mark.parametrize("text", [
    f"refused {TOKEN}",
    f"Authorization: Bearer {TOKEN}",
    f'{{"authorization": "{TOKEN}"}}',
    {"authorization": "opaque-value", "server_label": "alek_memory"},
    f"authorization=opaque-value at wss://api.example.test/v1/realtime?model=m and https://mcp.example.test/mcp",
])
def test_tokens_auth_values_and_urls_are_replaced(text):
    out = redact_for_log(text)

    assert TOKEN not in out and "opaque-value" not in out and "example.test" not in out


def test_plain_text_passes_unchanged():
    assert redact_for_log("Cancelled by cancel scope") == "Cancelled by cancel scope"


def test_long_text_is_cut_after_redaction():
    out = redact_for_log("x" * 1000 + TOKEN)

    assert len(out) <= 301 and TOKEN not in out
