import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from src.composition.slack_adapter_factory import SlackAdapterFactory


@pytest.mark.requirement("REQ-CORE-09")
def test_factory_selects_socket_mode_with_dev_tokens():
    """
    Verify Socket Mode adapter selection and DEV token override.
    Covers: REQ-CORE-09 (Adapter Mode Selection)
    """
    app = AsyncMock()
    agent_factory = AsyncMock()
    iam_service = AsyncMock()

    env_config = MagicMock()
    env_config.is_socket_mode = True
    env_config.is_http_mode = False
    env_config.slack_mode = MagicMock(value="socket")

    config = {
        "SLACK_BOT_TOKEN": "prod-bot",
        "SLACK_APP_TOKEN": "prod-app",
        "DEV_SLACK_BOT_TOKEN": "dev-bot",
        "DEV_SLACK_APP_TOKEN": "dev-app",
    }

    with patch("src.composition.slack_adapter_factory.SocketModeAdapter") as mock_socket, \
         patch("src.composition.slack_adapter_factory.ConversationHandler"):
        SlackAdapterFactory.create_adapter(
            app=app,
            coordinator=AsyncMock(),
            agent_factory=agent_factory,
            iam_service=iam_service,
            file_service=AsyncMock(),
            session_store=AsyncMock(),
            config=config,
            env_config=env_config,
            db_client=None
        )

        _, kwargs = mock_socket.call_args
        assert kwargs["config"]["SLACK_BOT_TOKEN"] == "dev-bot"
        assert kwargs["config"]["SLACK_APP_TOKEN"] == "dev-app"


@pytest.mark.requirement("REQ-CORE-09")
def test_factory_selects_http_mode_with_dependencies():
    """
    Verify HTTP Mode adapter selection and dependency wiring.
    Covers: REQ-CORE-09 (Adapter Mode Selection)
    """
    app = AsyncMock()
    agent_factory = AsyncMock()
    iam_service = AsyncMock()

    env_config = MagicMock()
    env_config.is_socket_mode = False
    env_config.is_http_mode = True
    env_config.is_development = False
    env_config.firestore_collection_prefix = ""
    env_config.event_dedup_collection = "dedup"
    env_config.slack_mode = MagicMock(value="http")

    config = {
        "GOOGLE_CLOUD_PROJECT": "proj",
        "CLOUD_RUN_SERVICE_URL": "http://localhost:8080",
        "SERVICE_ACCOUNT_EMAIL": "service@example.com",
        "SLACK_BOT_TOKEN": "prod-bot"
    }

    with patch("src.composition.slack_adapter_factory.GcpTaskQueue") as mock_tasks, \
         patch("src.composition.slack_adapter_factory.HTTPModeAdapter") as mock_http, \
         patch("src.composition.slack_adapter_factory.FirestoreEventDedupStore"), \
         patch("src.composition.slack_adapter_factory.ConversationHandler"):
        SlackAdapterFactory.create_adapter(
            app=app,
            coordinator=AsyncMock(),
            agent_factory=agent_factory,
            iam_service=iam_service,
            file_service=AsyncMock(),
            session_store=MagicMock(),
            config=config,
            env_config=env_config,
            db_client=MagicMock()
        )
        mock_tasks.assert_called_once()
        mock_http.assert_called_once()
