"""The send_to_chat worker hop keeps the call's turn id (core contract §6; review part A I2): the Cloud Task body is the
JSON of the delegation context (GcpTaskQueue's `_DomainEncoder`), AgentWorkerHandler passes that context to the Alek
gateway, and the gateway routes the same `turn_id` to Router → Smart on every delivery — so a retried errand meets the
undo re-run guard with the id of its first attempt."""
import json
from unittest.mock import AsyncMock, MagicMock

from src.adapters.gcp_task_queue import _DomainEncoder
from src.agents.alek_gateway_agent import AlekGatewayAgent
from src.domain.agent import AgentResponse
from src.domain.messaging import SmartResponse
from src.domain.turn_identity import TURN_ID_CONTEXT_KEY
from src.handlers.agent_worker_handler import AgentWorkerHandler

_PAYLOAD = {"task_type": "agent_execution", "agent_id": "alek_agent", "intent": "send_to_chat",
            "query": "note the PDF from yesterday as the contractor's invoice",
            "context": {"user_id": "u1", "account_id": "a1", "session_id": "u1:D9", "origin_channel_id": "D9",
                        "origin_platform": "slack", TURN_ID_CONTEXT_KEY: "voice:x",
                        "params": {"call_context": [{"role": "user", "text": "the one from the contractor"}]}}}


async def test_the_same_payload_delivered_twice_routes_the_same_turn_id():
    gateway = AlekGatewayAgent(config=MagicMock(agent_id="alek_agent_u1", timeout_ms=None),
                               notification_service=AsyncMock())
    smart = MagicMock()
    smart.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t", agent_id="smart", result=SmartResponse(text="Noted.", structured_data=None, link_list=[])))
    gateway.coordinator = smart
    worker_coordinator = MagicMock()
    worker_coordinator.route_message = AsyncMock(side_effect=gateway.execute)
    worker = AgentWorkerHandler(coordinator=worker_coordinator, notification_service=AsyncMock(),
                                task_queue=AsyncMock(), doc_delivery_service=None)
    body = json.loads(json.dumps(_PAYLOAD, cls=_DomainEncoder))          # what the Cloud Task carries
    for _ in range(2):
        assert (await worker.handle_task(json.loads(json.dumps(body))))["status"] == "success"
    first, second = (c.args[0] for c in smart.route_message.await_args_list)
    assert first.context[TURN_ID_CONTEXT_KEY] == second.context[TURN_ID_CONTEXT_KEY] == "voice:x"
    assert first.task_id != second.task_id                                # a fallback id would have differed
