"""
Unit test for EnvironmentConfig.companion_extraction_queue_collection property.

Verifies the collection name is properly prefixed based on environment.
"""
import os
from unittest.mock import patch

from src.config.environment import EnvironmentConfig


class TestCompanionExtractionQueueCollection:

    def test_companion_extraction_queue_collection_dev(self):
        # Development environment should have prefix
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            env = EnvironmentConfig()
            assert env.companion_extraction_queue_collection == "development_companion_extraction_queue"

    def test_companion_extraction_queue_collection_production(self):
        # Production environment should have no prefix
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            env = EnvironmentConfig()
            assert env.companion_extraction_queue_collection == "companion_extraction_queue"

    def test_companion_extraction_queue_collection_test(self):
        # Test environment should have prefix
        with patch.dict(os.environ, {"APP_ENV": "test"}, clear=True):
            env = EnvironmentConfig()
            assert env.companion_extraction_queue_collection == "test_companion_extraction_queue"
