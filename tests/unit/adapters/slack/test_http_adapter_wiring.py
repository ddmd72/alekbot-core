"""
Wiring test for HTTPModeAdapter — verifies DI contract.

Checks:
- HTTPModeAdapter accepts session_store via constructor and takes no dedup_store (S1b: the intake
  names its Cloud Task instead of marking the event)
- No direct Firestore adapter imports remain in http_adapter module
- Constructor param types are port ABCs (SessionStore), not concrete Firestore classes
"""
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.slack import http_adapter as http_adapter_module
from src.ports.session_store import SessionStore


class TestHTTPAdapterWiring:
    def test_no_firestore_session_store_import(self):
        """http_adapter must not import FirestoreSessionStore directly."""
        source = inspect.getsource(http_adapter_module)
        assert "FirestoreSessionStore" not in source

    def test_no_firestore_dedup_store_import(self):
        """http_adapter must not import FirestoreEventDedupStore directly."""
        source = inspect.getsource(http_adapter_module)
        assert "FirestoreEventDedupStore" not in source

    def test_constructor_accepts_session_store_port(self):
        """Constructor type annotation for session_store must be SessionStore port."""
        from src.adapters.slack.http_adapter import HTTPModeAdapter
        hints = HTTPModeAdapter.__init__.__annotations__
        assert hints.get("session_store") is SessionStore

    def test_constructor_takes_no_dedup_store(self):
        """S1b: the intake hands off through a named task (ALREADY_EXISTS = already queued), so the
        adapter has no dedup store to consult; the parameter is gone from the constructor."""
        from src.adapters.slack.http_adapter import HTTPModeAdapter
        assert "dedup_store" not in inspect.signature(HTTPModeAdapter.__init__).parameters
