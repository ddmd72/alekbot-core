import pytest
import os
from unittest.mock import MagicMock, patch
from src.config.settings import load_settings

@pytest.mark.requirement("REQ-SEC-01")
def test_secret_management_cloud_fetch_logic():
    """
    Integration test for REQ-SEC-01 (Secret Management).
    Verifies that load_settings attempts to fetch missing secrets from Secret Manager.
    """
    # Mock environment variables
    env_vars = {
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "APP_ENV": "production",
        "SLACK_BOT_TOKEN": "", # Missing
        "DEV_SLACK_BOT_TOKEN": "local-dev-token", # Present but should be ignored for cloud fetch
        "GEMINI_API_KEY": "env-key" # Present
    }
    
    with patch.dict(os.environ, env_vars), \
         patch("src.config.settings.secretmanager.SecretManagerServiceClient") as mock_client_class:
        
        mock_client = mock_client_class.return_value
        
        # Mock successful secret fetch
        mock_response = MagicMock()
        mock_response.payload.data.decode.return_value = "cloud-secret-token"
        mock_client.access_secret_version.return_value = mock_response
        
        settings = load_settings()
        
        # 1. Verify missing SLACK_BOT_TOKEN was fetched from cloud
        assert settings["SLACK_BOT_TOKEN"] == "cloud-secret-token"
        
        # 2. Verify GEMINI_API_KEY was NOT fetched (already in env)
        # We check if access_secret_version was called with GEMINI_API_KEY
        # Note: access_secret_version is called with keyword argument 'request'
        calls = [call.kwargs['request']['name'] for call in mock_client.access_secret_version.call_args_list]
        assert any("SLACK_BOT_TOKEN" in c for c in calls)
        assert not any("GEMINI_API_KEY" in c for c in calls)
        
        # 3. Verify DEV_ tokens were NOT attempted to be fetched from cloud even if missing
        assert not any("DEV_SLACK_BOT_TOKEN" in c for c in calls)

@pytest.mark.requirement("REQ-SEC-01")
def test_secret_management_skips_cloud_in_emulator():
    """
    Verify that Secret Manager is NOT called when using Firestore emulator.
    """
    env_vars = {
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "FIRESTORE_EMULATOR_HOST": "localhost:8080",
        "SLACK_BOT_TOKEN": ""
    }
    
    with patch.dict(os.environ, env_vars), \
         patch("src.config.settings.secretmanager.SecretManagerServiceClient") as mock_client_class:
        
        settings = load_settings()
        
        # Client should not even be instantiated
        assert not mock_client_class.called
        assert settings["SLACK_BOT_TOKEN"] == ""
