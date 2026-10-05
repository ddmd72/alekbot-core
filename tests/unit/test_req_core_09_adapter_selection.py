import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from src.composition.slack_adapter_factory import SlackAdapterFactory


@pytest.mark.requirement("REQ-CORE-09")
def test_factory_requires_db_client():
    """Socket Mode was the only adapter that ran without persistence.

    It was removed 2026-08-16 (local-only, unused — everything runs on Cloud Run), so the
    factory no longer selects between modes: HTTP is the only path, and it cannot be built
    without a session store.
    Covers: REQ-CORE-09 (Adapter Mode Selection)
    """
    with patch("src.composition.slack_adapter_factory.ConversationHandler"):
        with pytest.raises(ValueError, match="db_client is required"):
            SlackAdapterFactory.create_adapter(
                app=AsyncMock(),
                coordinator=AsyncMock(),
                agent_factory=AsyncMock(),
                iam_service=AsyncMock(),
                file_service=AsyncMock(),
                session_store=AsyncMock(),
                config={"SLACK_BOT_TOKEN": "prod-bot"},
                env_config=MagicMock(),
                db_client=None,
            )


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


@pytest.mark.requirement("REQ-CORE-09")
def test_factory_forwards_skill_service_to_conversation_handler():
    """SlackAdapterFactory must thread its skill_service kwarg into ConversationHandler
    unchanged — composition wiring for Agent Skills delivery (Task 8)."""
    app = AsyncMock()
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
        "SLACK_BOT_TOKEN": "prod-bot",
    }
    sentinel_skill_service = MagicMock(name="skill_service")

    with patch("src.composition.slack_adapter_factory.GcpTaskQueue"), \
         patch("src.composition.slack_adapter_factory.HTTPModeAdapter"), \
         patch("src.composition.slack_adapter_factory.FirestoreEventDedupStore"), \
         patch("src.composition.slack_adapter_factory.ConversationHandler") as mock_handler:
        SlackAdapterFactory.create_adapter(
            app=app,
            coordinator=AsyncMock(),
            agent_factory=AsyncMock(),
            iam_service=AsyncMock(),
            file_service=AsyncMock(),
            session_store=MagicMock(),
            config=config,
            env_config=env_config,
            db_client=MagicMock(),
            skill_service=sentinel_skill_service,
        )
        assert mock_handler.call_args.kwargs["skill_service"] is sentinel_skill_service
