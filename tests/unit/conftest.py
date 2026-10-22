import pytest

from tests.support import network_guard


@pytest.fixture(autouse=True)
def _offline_unit_suite(monkeypatch):
    """H3: a unit test never leaves the machine. See tests/support/network_guard.py for the rule and the escape."""
    network_guard.install(monkeypatch)
    yield
