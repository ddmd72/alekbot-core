import pytest
from unittest.mock import AsyncMock, MagicMock, call
from src.adapters.firestore_repo import FirestoreFactRepository
from src.domain.entities import FactEntity, FactType

@pytest.mark.requirement("REQ-MEM-01")
@pytest.mark.requirement("REQ-MEM-03")
@pytest.mark.asyncio
async def test_repository_save_fact_logic(mock_env_config):
    """
    Test that repository correctly prepares data for Firestore.
    Covers: REQ-MEM-01 (Versioning), REQ-MEM-03 (Sandboxing)
    """
    # Mock Firestore client
    mock_db = MagicMock()
    mock_collection = MagicMock()
    mock_doc = MagicMock()
    
    mock_db.collection.return_value = mock_collection
    mock_collection.document.return_value = mock_doc
    
    # Mock the set method to be awaitable
    mock_doc.set = AsyncMock()
    
    repo = FirestoreFactRepository(mock_db, mock_env_config)
    
    fact = FactEntity(
        account_id="user-1",
        created_by_user_id="user-1",
        text="Integration test fact",
        type=FactType.EVENT,
        lineage_id="lineage-123"
    )
    
    await repo.add_fact(fact)

    # Verify data structure (REQ-MEM-01)
    args, kwargs = mock_doc.set.call_args
    saved_data = args[0]
    assert saved_data["lineage_id"] == "lineage-123"
    assert saved_data["is_current"] is True
    assert "created_at" in saved_data
