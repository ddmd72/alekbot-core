"""
Wiring test for HTTPModeAdapter — verifies DI contract.

Checks:
- HTTPModeAdapter accepts session_store and dedup_store via constructor
- No direct Firestore adapter imports remain in http_adapter module
- Constructor param types are port ABCs (SessionStore, DedupStore), not concrete Firestore classes
"""
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.slack import http_adapter as http_adapter_module
from src.ports.session_store import SessionStore
from src.ports.dedup_store import DedupStore


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

    def test_constructor_accepts_dedup_store_port(self):
        """Constructor type annotation for dedup_store must be DedupStore port."""
        from src.adapters.slack.http_adapter import HTTPModeAdapter
        hints = HTTPModeAdapter.__init__.__annotations__
        assert hints.get("dedup_store") is DedupStore
