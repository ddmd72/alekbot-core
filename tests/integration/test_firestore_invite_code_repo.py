import pytest
from unittest.mock import MagicMock, AsyncMock, ANY
from datetime import datetime, timezone, timedelta
from src.domain.invite_code import InviteCode, InviteType
from src.adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from src.config.environment import EnvironmentConfig

@pytest.fixture
def mock_db():
    db = MagicMock()
    # Mock collection
    collection = MagicMock()
    db.collection.return_value = collection
    
    # Mock document
    document = MagicMock()
    collection.document.return_value = document
    
    # Mock set (async)
    document.set = AsyncMock()
    
    # Mock get (async)
    document.get = AsyncMock()
    
    # Mock query
    query = MagicMock()
    collection.where.return_value = query
    query.stream = MagicMock()
    
    return db

@pytest.fixture
def mock_env_config():
    config = MagicMock(spec=EnvironmentConfig)
    config.domain_invite_codes_collection = "test_invite_codes"
    return config

@pytest.fixture
def repo(mock_db, mock_env_config):
    return FirestoreInviteCodeRepository(mock_db, mock_env_config)

@pytest.mark.asyncio
async def test_create_invite_code(repo, mock_db):
    """Test creating an invite code."""
    invite = InviteCode(
        code="ABC-123",
        user_id="user-1",
        account_id="acc-1",
        type=InviteType.SELF_LINK,
        expires_at=datetime.now(timezone.utc),
        platform="slack"
    )
    
    await repo.create(invite)
    
    # Verify DB interactions
    mock_db.collection.assert_called_with("test_invite_codes")
    mock_db.collection().document.assert_called_with("ABC-123")
    mock_db.collection().document().set.assert_called_once()
    
    # Verify data format
    data = mock_db.collection().document().set.call_args[0][0]
    assert data["code"] == "ABC-123"
    assert data["type"] == "self_link"
    assert data["platform"] == "slack"

@pytest.mark.asyncio
async def test_get_by_code_found(repo, mock_db):
    """Test retrieving an existing invite code."""
    # Setup mock return
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "code": "XYZ-987",
        "user_id": "user-2",
        "account_id": "acc-2",
        "type": "team_invite",
        "expires_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
        "role": "MEMBER"
    }
    mock_db.collection().document().get.return_value = mock_doc
    
    # Execute
    result = await repo.get_by_code("XYZ-987")
    
    # Verify
    assert result is not None
    assert result.code == "XYZ-987"
    assert result.type == InviteType.TEAM_INVITE
    assert result.role == "MEMBER"

@pytest.mark.asyncio
async def test_get_by_code_not_found(repo, mock_db):
    """Test retrieving a non-existent code."""
    # Setup mock return
    mock_doc = MagicMock()
    mock_doc.exists = False
    mock_db.collection().document().get.return_value = mock_doc
    
    # Execute
    result = await repo.get_by_code("MISSING")
    
    # Verify
    assert result is None

@pytest.mark.asyncio
async def test_list_by_user(repo, mock_db):
    """Test listing codes by user."""
    # Setup mock stream
    mock_doc1 = MagicMock()
    mock_doc1.to_dict.return_value = {
        "code": "C1",
        "user_id": "u1",
        "account_id": "a1",
        "type": "self_link",
        "expires_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc)
    }
    
    async def async_stream():
        yield mock_doc1
        
    mock_db.collection().where().stream.return_value = async_stream()
    
    # Execute
    results = await repo.list_by_user("u1")
    
    # Verify
    assert len(results) == 1
    assert results[0].code == "C1"
    mock_db.collection().where.assert_called_with(filter=ANY)
