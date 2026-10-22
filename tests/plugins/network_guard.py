"""The offline guard for runs outside tests/unit/ that must stay offline: `make test-g2-offline` passes
`-p tests.plugins.network_guard`. (tests/unit/ gets the same guard from tests/unit/conftest.py; a plugin rather than
tests/integration/conftest.py because most of tests/integration/ talks to real services on purpose.)"""

import pytest

from tests.support import network_guard


@pytest.fixture(autouse=True)
def _offline_guard(monkeypatch):
    network_guard.install(monkeypatch)
    yield
