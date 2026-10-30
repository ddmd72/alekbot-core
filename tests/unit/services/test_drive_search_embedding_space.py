"""The card stream answers only queries embedded in the space its cards are in (USER_DRIVE_INDEXING_RFC §12.3,
§10.1 step 2). While the index moves to a new embedding space, search runs the name stream and the filters alone and
says the index is being refreshed: a cosine across two spaces ranks noise."""
import pytest
from src.domain.drive_search import DriveSearchRequest, render_results
from src.services.drive_search_service import DriveSearchService

from .test_drive_search_service import World


def _service(w, space):
    return DriveSearchService(w.repo, w.emb, w.users, w.drive, clock=w.clock, nonce=lambda: "nonce0001",
                              embedding_space=space)


async def _world():
    w = World()   # the indexer embeds cards in "test-space/8"
    w.drive.add("lease agreement.pdf")
    w.drive.add("budget.ods")
    await w.settle()
    return w


async def test_a_new_space_skips_the_card_stream_and_says_refreshing():
    w = await _world()
    svc = _service(w, "next-model/8")
    calls = len(w.emb.calls)
    result = await svc.search("u", DriveSearchRequest(query="lease"))
    assert [c.name for c in result.cards] == ["lease agreement.pdf"]   # the name stream still answers
    assert len(w.emb.calls) == calls and w.repo.card_limits == []       # no query embedding, no kNN
    assert result.envelope.refreshing
    assert "being refreshed" in render_results(result)


async def test_the_folder_ranking_in_memory_skips_card_vectors_too():
    w = World()
    notes = w.drive.add("Notes", folder=True)
    w.drive.add("plan.md", parent=notes)
    await w.settle()
    svc = _service(w, "next-model/8")
    calls = len(w.emb.calls)
    result = await svc.search("u", DriveSearchRequest(query="plan", folder="Notes"))
    assert [c.name for c in result.cards] == ["plan.md"] and len(w.emb.calls) == calls
    assert result.envelope.refreshing


async def test_the_current_space_uses_the_card_stream():
    w = await _world()
    result = await _service(w, "test-space/8").search("u", DriveSearchRequest(query="lease"))
    assert w.repo.card_limits and not result.envelope.refreshing
    assert "being refreshed" not in render_results(result)


async def test_filter_only_is_unaffected_but_still_says_refreshing():
    w = await _world()
    result = await _service(w, "next-model/8").search("u", DriveSearchRequest(kind="pdf"))
    assert [c.name for c in result.cards] == ["lease agreement.pdf"] and result.envelope.refreshing


async def test_onboarding_is_building_not_refreshing():
    """A minimal state has no space yet: it is onboarding (building), never a refresh."""
    from src.domain.drive_index import DriveSyncState
    w = World()
    assert await w.repo.create_sync_state_if_absent(DriveSyncState(user_id="u", connection_id="u", created_at=w.clock()))
    result = await _service(w, "test-space/8").search("u", DriveSearchRequest(query="lease"))
    assert result.envelope.building and not result.envelope.refreshing


def test_production_and_the_eval_pin_the_query_space(monkeypatch):
    """The main service embeds queries with GeminiEmbeddingAdapter: it checks the cards against that space."""
    from unittest.mock import MagicMock

    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
    from src.composition import service_container
    from src.ports.user_drive_port import UserDrivePort
    from tests.unit.composition.test_drive_search_wiring import _container

    monkeypatch.setattr(service_container, "OneDriveAdapter", lambda **kw: MagicMock(spec=UserDrivePort))
    c = _container(monkeypatch, MICROSOFT_TODO_CLIENT_ID="cid", MICROSOFT_TODO_CLIENT_SECRET="sec")
    assert c.drive_search_service._space == GeminiEmbeddingAdapter.EMBEDDING_SPACE

    import inspect
    from scripts.validation.drive_eval import runner
    assert "embedding_space=embedding_space" in inspect.getsource(runner.run_eval)


def test_the_query_space_is_required():
    """No default that skips the check: a wiring that forgets the space must fail at construction."""
    import inspect

    import pytest
    param = inspect.signature(DriveSearchService.__init__).parameters["embedding_space"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is inspect.Parameter.empty
    w = World()
    with pytest.raises(TypeError):
        DriveSearchService(w.repo, w.emb, w.users, w.drive)
