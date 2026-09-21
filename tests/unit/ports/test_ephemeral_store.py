import inspect
from src.ports.ephemeral_store import EphemeralStore


def test_ephemeral_store_is_abstract():
    assert inspect.isabstract(EphemeralStore)


def test_ephemeral_store_declares_expected_methods():
    assert {"set", "get", "delete"}.issubset(set(dir(EphemeralStore)))
