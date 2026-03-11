import pytest
from unittest.mock import AsyncMock
from quart import Quart
from src.adapters.slack.http_adapter import HTTPModeAdapter


@pytest.fixture
def http_adapter():
    return HTTPModeAdapter(
        app=AsyncMock(),
        config={"SLACK_SIGNING_SECRET": "test_secret", "SLACK_BOT_TOKEN": "xoxb"},
        task_service=AsyncMock(),
        session_store=AsyncMock(),
        conversation_handler=AsyncMock(),
        iam_service=AsyncMock(),
        dedup_store=AsyncMock(),
    )


@pytest.fixture
def quart_app(http_adapter):
    app = Quart(__name__)
    app.register_blueprint(http_adapter.blueprint)
    return app


@pytest.mark.requirement("REQ-CORE-08")
@pytest.mark.asyncio
async def test_worker_task_rejects_empty_payload(quart_app, http_adapter):
    """
    Verify worker rejects empty payload.
    Covers: REQ-CORE-08 (HTTP Event Validation)
    """
    async with quart_app.test_request_context(
        "/worker",
        method="POST",
        json=None
    ):
        response, status = await http_adapter._handle_worker_task()
        result = await response.get_json()

    assert status == 400
    assert result["error"] == "Empty payload"


@pytest.mark.requirement("REQ-CORE-08")
@pytest.mark.asyncio
async def test_worker_task_rejects_missing_fields(quart_app, http_adapter):
    """
    Verify worker rejects payloads missing required fields.
    Covers: REQ-CORE-08 (HTTP Event Validation)
    """
    async with quart_app.test_request_context(
        "/worker",
        method="POST",
        json={"event": {}}
    ):
        response, status = await http_adapter._handle_worker_task()
        result = await response.get_json()

    assert status == 400
    assert result["error"] == "Missing required fields"
