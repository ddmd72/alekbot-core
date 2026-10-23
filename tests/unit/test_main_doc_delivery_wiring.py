"""One `DocumentDeliveryService` serves the async worker and both chat handlers in the deployed graph (P9 T3, D1).

Built on the T1 boot (`tests/unit/test_main_boots_offline.py`): the instance `AgentWorkerHandler` delivers documents
with is the very instance the Slack and the Telegram `ConversationHandler` hold, and it exists (the deploy has a media
bucket).
"""
import pytest

from tests.unit.test_main_boots_offline import _boot

pytestmark = pytest.mark.slow


async def test_the_one_doc_delivery_service_reaches_both_handlers_and_the_worker(monkeypatch, caplog):
    boot = await _boot(monkeypatch, caplog)
    if boot.probe_error is not None:
        raise boot.probe_error

    worker = boot.worker_closure["worker_handler"]
    service = worker._agent_worker._doc_delivery_service
    assert service is not None
    assert worker._slack_worker.__self__.conversation_handler._doc_delivery_service is service
    assert worker._telegram_worker.__self__.conversation_handler._doc_delivery_service is service
