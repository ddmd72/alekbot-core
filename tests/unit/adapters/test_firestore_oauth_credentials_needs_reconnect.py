"""
FirestoreOAuthCredentialsAdapter — the needs_reconnect flag round-trips, and
documents written before the flag existed read as False.
"""
from datetime import datetime, timezone

from src.adapters.firestore_oauth_credentials_adapter import FirestoreOAuthCredentialsAdapter
from src.domain.email import OAuthCredentials


def _creds(flag: bool) -> OAuthCredentials:
    return OAuthCredentials(
        user_id="user-1",
        provider="gmail",
        access_token="tok",
        refresh_token="rtok",
        token_expiry=datetime(2026, 10, 10, 11, 12, tzinfo=timezone.utc),
        scopes=[],
        email_address="user@example.com",
        needs_reconnect=flag,
    )


def test_flag_round_trips():
    for flag in (True, False):
        doc = FirestoreOAuthCredentialsAdapter._to_firestore(_creds(flag))
        assert doc["needs_reconnect"] is flag
        assert FirestoreOAuthCredentialsAdapter._from_firestore(doc).needs_reconnect is flag


def test_legacy_document_without_flag_reads_false():
    doc = FirestoreOAuthCredentialsAdapter._to_firestore(_creds(False))
    del doc["needs_reconnect"]
    assert FirestoreOAuthCredentialsAdapter._from_firestore(doc).needs_reconnect is False
