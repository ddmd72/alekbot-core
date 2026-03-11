import pytest
from unittest.mock import MagicMock, AsyncMock
from src.adapters.firestore_repo import FirestoreFactRepository
from src.config.environment import EnvironmentConfig

@pytest.mark.requirement("REQ-CORE-02")
@pytest.mark.asyncio
async def test_production_safety_blocks_destructive_ops():
    """
    Integration test for REQ-CORE-02 (Environment Safety).
    Verifies that destructive operations are blocked in production.
    """
    # 1. Setup Production Config
    mock_env = MagicMock(spec=EnvironmentConfig)
    mock_env.is_production = True
    mock_env.firestore_collection_prefix = ""
    
    mock_db = MagicMock()
    repo = FirestoreFactRepository(mock_db, mock_env)
    
    # 2. Attempt destructive operation
    with pytest.raises(PermissionError) as excinfo:
        await repo.archive_observations(["obs-1"], owner_id="user-1")
    
    assert "blocked in PRODUCTION environment" in str(excinfo.value)
    # Verify no DB calls were made to delete
    assert not mock_db.collection().document().delete.called

@pytest.mark.requirement("REQ-CORE-02")
@pytest.mark.asyncio
async def test_production_safety_allows_ops_in_dev():
    """
    Verify that destructive operations ARE allowed in development.
    """
    # 1. Setup Development Config
    mock_env = MagicMock(spec=EnvironmentConfig)
    mock_env.is_production = False
    mock_env.firestore_collection_prefix = "dev_"
    
    mock_db = MagicMock()
    mock_doc = MagicMock()
    mock_doc.get = AsyncMock(return_value=MagicMock(exists=True, to_dict=lambda: {"id": "obs-1", "owner_id": "user-1"}))
    mock_doc.set = AsyncMock()
    mock_doc.delete = AsyncMock()
    
    mock_db.collection.return_value.document.return_value = mock_doc
    
    repo = FirestoreFactRepository(mock_db, mock_env)
    
    # 2. Execute operation
    batch_mock = AsyncMock()
    batch_mock.commit = AsyncMock()
    mock_db.batch.return_value = batch_mock
    
    await repo.archive_observations(["obs-1"], owner_id="user-1")
    
    # 3. Verify DB calls were made
    batch_mock.commit.assert_called_once()
