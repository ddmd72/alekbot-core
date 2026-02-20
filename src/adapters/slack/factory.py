"""
Slack Adapter Factory
Creates appropriate adapter based on configuration
"""
from typing import Optional
from slack_bolt.async_app import AsyncApp

from .base import SlackAdapter
from .socket_adapter import SocketModeAdapter
from .http_adapter import HTTPModeAdapter
from ...adapters.gcp_task_queue import GcpTaskQueue
from ...adapters.firestore_session_store import FirestoreSessionStore
from ...adapters.firestore_dedup_store import FirestoreEventDedupStore
from ...config.environment import EnvironmentConfig
from ...utils.logger import logger
from ...infrastructure.agent_coordinator import AgentCoordinator
from ...services.user_agent_factory import UserAgentFactory
from ...services.iam_service import IAMService
from ...ports.file_service import FileService


class SlackAdapterFactory:
    """
    Factory for creating Slack adapters based on configuration.
    Implements Factory pattern for dual-mode support.
    
    Updated (2026-02-05): Replaced IdentityResolver with IAMService.
    """

    @staticmethod
    def create_adapter(
        app: AsyncApp,
        config: dict,
        env_config: EnvironmentConfig,
        coordinator: AgentCoordinator,
        agent_factory: UserAgentFactory,
        iam_service: IAMService,
        file_service: FileService,
        session_store: FirestoreSessionStore,
        db_client=None,
        consolidation_queue=None,
        consolidation_config=None,
        audio_service=None,
    ) -> SlackAdapter:
        """
        Create appropriate Slack adapter based on environment configuration.

        Args:
            app: Slack Bolt AsyncApp instance
            config: Configuration dictionary
            env_config: EnvironmentConfig instance
            coordinator: AgentCoordinator instance
            agent_factory: Factory for creating per-user agents
            iam_service: IAMService for centralized authorization
            file_service: FileService instance
            db_client: Firestore AsyncClient (required for HTTP mode)

        Returns:
            SlackAdapter instance (either SocketModeAdapter or HTTPModeAdapter)
        """
        mode = env_config.slack_mode.value

        logger.info(f"🏭 Creating Slack adapter: {mode}")

        if env_config.is_socket_mode:
            socket_config = config.copy()
            if config.get("DEV_SLACK_BOT_TOKEN"):
                socket_config["SLACK_BOT_TOKEN"] = config["DEV_SLACK_BOT_TOKEN"]
            if config.get("DEV_SLACK_APP_TOKEN"):
                socket_config["SLACK_APP_TOKEN"] = config["DEV_SLACK_APP_TOKEN"]

            return SocketModeAdapter(
                app=app,
                config=socket_config,
                coordinator=coordinator,
                agent_factory=agent_factory,
                iam_service=iam_service,
                file_service=file_service,
                consolidation_queue=consolidation_queue,
                consolidation_config=consolidation_config,
                audio_service=audio_service,
            )

        if env_config.is_http_mode:
            if not db_client:
                raise ValueError("db_client is required for HTTP mode (session persistence)")

            # ADR-006: Use semantic collection name
            dedup_store = FirestoreEventDedupStore(
                db_client=db_client,
                collection_prefix=env_config.event_dedup_collection
            )

            queue_suffix = "dev" if env_config.is_development else "prod"

            service_url = config.get("CLOUD_RUN_SERVICE_URL")
            if not service_url:
                logger.warning("⚠️ CLOUD_RUN_SERVICE_URL not set, defaulting to http://localhost:8080")
                service_url = "http://localhost:8080"

            task_service = GcpTaskQueue(
                project_id=config["GOOGLE_CLOUD_PROJECT"],
                location="europe-west1",
                queue_name=f"alek-bot-tasks-{queue_suffix}",
                service_url=service_url,
                service_account_email=config.get("SERVICE_ACCOUNT_EMAIL")
            )

            return HTTPModeAdapter(
                app=app,
                config=config,
                task_service=task_service,
                session_store=session_store,
                coordinator=coordinator,
                agent_factory=agent_factory,
                iam_service=iam_service,
                dedup_store=dedup_store,
                file_service=file_service,
                consolidation_queue=consolidation_queue,
                consolidation_config=consolidation_config,
                audio_service=audio_service,
            )

        raise ValueError(f"Unknown Slack mode: {mode}")
