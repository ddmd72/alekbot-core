"""WorkspaceStorePort: four methods, nothing else (AGENT_WORKSPACE_RFC §13 — no stat / versions / purge)."""
import inspect

from src.ports.workspace_store_port import WorkspaceStorePort


def test_four_abstract_methods():
    assert WorkspaceStorePort.__abstractmethods__ == {"list", "read", "put", "delete"}


def test_signatures():
    params = lambda m: list(inspect.signature(getattr(WorkspaceStorePort, m)).parameters)  # noqa: E731
    assert params("list") == ["self", "user_id", "prefix", "include_versions"]
    assert params("read") == ["self", "user_id", "path", "generation"]
    assert params("put") == ["self", "user_id", "path", "data", "meta", "if_generation_match"]
    assert params("delete") == ["self", "user_id", "path", "if_generation_match", "meta"]
    assert inspect.signature(WorkspaceStorePort.list).parameters["include_versions"].default is False
    assert inspect.signature(WorkspaceStorePort.read).parameters["generation"].default is None


def test_fake_implements_the_port():
    from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore
    assert isinstance(InMemoryWorkspaceStore(), WorkspaceStorePort)
