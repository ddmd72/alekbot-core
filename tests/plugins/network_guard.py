"""The offline guard for runs outside tests/unit/ that must stay offline: `make test-integration` (tests/integration/offline/)
and `make test-g2-offline` pass `-p tests.plugins.network_guard`. (tests/unit/ gets the same guard from tests/unit/conftest.py;
a plugin keeps the guard opt-in per command rather than tied to a folder's conftest.)"""

import pytest

from tests.support import network_guard


@pytest.fixture(autouse=True)
def _offline_guard(monkeypatch):
    network_guard.install(monkeypatch)
    yield
