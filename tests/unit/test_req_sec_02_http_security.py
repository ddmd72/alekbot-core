import pytest
import time
import hmac
import hashlib
from unittest.mock import MagicMock, AsyncMock
from src.adapters.slack.http_adapter import HTTPModeAdapter

@pytest.fixture
def mock_http_adapter():
    """Create an HTTPModeAdapter with mocked dependencies."""
    return HTTPModeAdapter(
        app=AsyncMock(),
        config={"SLACK_SIGNING_SECRET": "test_secret", "SLACK_BOT_TOKEN": "xoxb-test"},
        task_service=AsyncMock(),
        session_store=AsyncMock(),
        coordinator=AsyncMock(),
        agent_factory=AsyncMock(),
        iam_service=AsyncMock(),
        dedup_store=AsyncMock(),
        file_service=AsyncMock()
    )

@pytest.mark.requirement("REQ-SEC-02")
def test_verify_signature_valid(mock_http_adapter):
    """
    Test Slack signature verification logic with valid signature.
    Covers: REQ-SEC-02 (Platform Integrity)
    """
    secret = "test_secret"
    timestamp = str(int(time.time()))
    body = b"event_data"
    
    # Calculate valid signature
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    signature = "v0=" + hmac.new(
        secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature
    }
    
    assert mock_http_adapter._verify_signature(body, headers) is True

@pytest.mark.requirement("REQ-SEC-02")
def test_verify_signature_invalid(mock_http_adapter):
    """
    Test Slack signature verification logic with invalid signature.
    Covers: REQ-SEC-02 (Platform Integrity)
    """
    headers = {
        "X-Slack-Request-Timestamp": str(int(time.time())),
        "X-Slack-Signature": "v0=invalid_hash"
    }
    
    assert mock_http_adapter._verify_signature(b"data", headers) is False

@pytest.mark.requirement("REQ-SEC-02")
def test_verify_signature_replay_attack(mock_http_adapter):
    """
    Test that old timestamps are rejected (Replay Attack protection).
    Covers: REQ-SEC-02 (Platform Integrity)
    """
    secret = "test_secret"
    # Timestamp from 10 minutes ago (limit is 5 mins)
    old_timestamp = str(int(time.time()) - 600)
    body = b"event_data"
    
    sig_basestring = f"v0:{old_timestamp}:{body.decode('utf-8')}"
    signature = "v0=" + hmac.new(
        secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        "X-Slack-Request-Timestamp": old_timestamp,
        "X-Slack-Signature": signature
    }
    
    assert mock_http_adapter._verify_signature(body, headers) is False
