"""create_router_agent forwards the fact repository and embedding service to the RouterAgent it builds."""
from src.agents.core.router_agent import create_router_agent


def test_factory_forwards_repository():
    repo = object()
    router = create_router_agent(repository=repo)  # type: ignore[arg-type]
    assert router.repository is repo


def test_factory_forwards_embedding_service():
    embedder = object()
    router = create_router_agent(embedding_service=embedder)  # type: ignore[arg-type]
    assert router.embedding_service is embedder
