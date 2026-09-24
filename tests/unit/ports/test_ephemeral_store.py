import inspect
from src.ports.ephemeral_store import EphemeralStore


def test_ephemeral_store_is_abstract():
    assert inspect.isabstract(EphemeralStore)


def test_ephemeral_store_declares_expected_methods():
    assert {"set", "get", "delete"}.issubset(set(dir(EphemeralStore)))

def test_ephemeral_store_declares_atomic_get_and_delete():
    """Single-use ticket consumption (voice_control_plane_app.session_config)
    needs read+delete as ONE step; a get()-then-delete() pair is a TOCTOU race
    between two concurrent redemptions of the same ticket."""
    assert "get_and_delete" in dir(EphemeralStore)
    assert getattr(EphemeralStore.get_and_delete, "__isabstractmethod__", False)
