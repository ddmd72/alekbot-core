import pytest
from unittest.mock import AsyncMock
from quart import Quart
from src.adapters.slack.http_adapter import HTTPModeAdapter


@pytest.fixture
def http_adapter():
    return HTTPModeAdapter(
        app=AsyncMock(),
        config={"SLACK_SIGNING_SECRET": "test_secret", "SLACK_BOT_TOKEN": "xoxb-test"},
        task_service=AsyncMock(),
        session_store=AsyncMock(),
        coordinator=AsyncMock(),
        agent_factory=AsyncMock(),
        iam_service=AsyncMock(),
        dedup_store=AsyncMock(),
        file_service=AsyncMock()
    )


@pytest.fixture
def quart_app(http_adapter):
    app = Quart(__name__)
    app.register_blueprint(http_adapter.blueprint)
    return app


@pytest.mark.requirement("REQ-CORE-05")
@pytest.mark.asyncio
@pytest.mark.skip(reason="Health endpoint moved to user_cabinet_app (web layer). HTTP adapter uses Blueprint only; no standalone health route. REQ-CORE-05 covered by integration tests.")
async def test_health_check_endpoint(quart_app):
    """
    Test the health check endpoint for operational readiness.
    Covers: REQ-CORE-05 (Operational Health)
    """
    async with quart_app.test_client() as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = await response.get_json()
        assert data["status"] == "healthy"
        assert data["mode"] == "http"
