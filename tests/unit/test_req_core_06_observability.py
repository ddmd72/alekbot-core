import pytest
import logging
from src.utils.logger import AlekFormatter

@pytest.mark.requirement("REQ-CORE-06")
def test_logger_formatter_no_timestamp():
    """
    Test that the custom formatter does NOT include timestamps (as Cloud Logging adds them).
    Covers: REQ-CORE-06 (Structured Observability)
    """
    formatter = AlekFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="Test message",
        args=(),
        exc_info=None
    )
    
    formatted_msg = formatter.format(record)
    
    # Should only contain the message, no timestamp prefix
    assert formatted_msg == "Test message"
    assert "202" not in formatted_msg  # No year
    assert ":" not in formatted_msg[0:5]  # No time at start
