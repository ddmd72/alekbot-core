import pytest
from unittest.mock import AsyncMock, MagicMock
from src.ports.llm_port import LLMPort
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
def mock_llm_port():
    return AsyncMock(spec=LLMPort)


@pytest.fixture
def mock_repository():
    return AsyncMock(spec=FactRepository)
