import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from src.ports.llm_service import LLMService
from src.ports.repository import FactRepository
from src.config.environment import EnvironmentConfig, Environment

@pytest.fixture
def mock_env_config():
    config = MagicMock(spec=EnvironmentConfig)
    config.env = Environment.TEST
    config.is_production = False
    config.is_development = False
    config.is_test = True
    config.firestore_collection_prefix = "test_"
    return config

@pytest.fixture
def mock_llm_service():
    mock = AsyncMock(spec=LLMService)
    # Explicitly add methods that might be missing from spec if not fully defined
    if not hasattr(mock, 'generate_response'):
        mock.generate_response = AsyncMock()
    return mock

@pytest.fixture
def mock_repository():
    mock = AsyncMock(spec=FactRepository)
    if not hasattr(mock, 'save_fact'):
        mock.save_fact = AsyncMock()
    return mock

@pytest.fixture(scope="session")
def event_loop():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    yield loop
    loop.close()
