"""
Integration tests for the document generation pipeline.

Tests the full chain:
  orchestrator
    → coordinator.handle_delegation(CREATE_DOCUMENT)          [ASYNC → task queue]
    → AgentWorkerHandler.handle_task()                         [Cloud Task callback]
    → coordinator.route_message(doc_planner_agent_{user_id})   [DELEGATE intent]
    → DocPlannerAgent.execute()                                 [LLM → JSON spec]
    → coordinator.handle_delegation(GENERATE_DOCX_CODE)        [SYNC → inline]
    → DocGeneratorAgent.execute()                               [LLM tool loop + DocxRunnerPort]
    → AgentResponse(delivery_items=[file_upload])
    → AgentWorkerHandler._deliver_docx_result()
    → notification.notify_file_bytes()

Real objects: AgentRegistry, AgentCoordinator, DocPlannerAgent, DocGeneratorAgent, AgentWorkerHandler.
Mocked: LLM providers, PromptBuilder, TaskQueue, NotificationService, DocxRunnerPort.
"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.doc_generator_agent import DocGeneratorAgent
from src.agents.doc_planner_agent import DocPlannerAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import LLMResponse, ToolCall
from src.domain.user import PerformanceTier
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import DOC_GENERATOR, DOC_PLANNER, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.ports.docx_runner_port import DocxRunnerError, DocxRunnerPort
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.task_queue import TaskQueue


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_ID = "user123"
_ACCOUNT_ID = "acc1"
_CONTEXT = {"user_id": _USER_ID, "account_id": _ACCOUNT_ID}

_VALID_SPEC = {
    "status": "ready",
    "task_summary": "Quarterly sales report",
    "doc_spec": {
        "document_type": "report",
        "title": "Sales Report Q1",
    },
}

_FAKE_DOCX_BYTES = b"PK\x03\x04fake-docx-content"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_task_queue():
    q = AsyncMock(spec=TaskQueue)
    q.enqueue_agent_task.return_value = "projects/test/queues/q/tasks/t1"
    return q


@pytest.fixture
def mock_notification():
    return AsyncMock()


@pytest.fixture
def mock_llm_planner():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = LLMResponse(
        text=json.dumps(_VALID_SPEC), tool_calls=[]
    )
    return m


@pytest.fixture
def mock_llm_generator():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = LLMResponse(
        text=None,
        tool_calls=[ToolCall(name="generate_docx", args={"js_code": "console.log('hi')"})],
    )
    return m


@pytest.fixture
def mock_docx_runner():
    r = AsyncMock(spec=DocxRunnerPort)
    r.run.return_value = _FAKE_DOCX_BYTES
    return r


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a specialist..."
    return pb


@pytest.fixture
def pipeline(
    mock_task_queue,
    mock_notification,
    mock_llm_planner,
    mock_llm_generator,
    mock_docx_runner,
    mock_prompt_builder,
):
    registry = AgentRegistry()
    registry.register(DOC_PLANNER)
    registry.register(DOC_GENERATOR)

    coordinator = AgentCoordinator(registry=registry, task_queue=mock_task_queue)

    planner_config = AgentConfig(
        agent_id=f"doc_planner_agent_{_USER_ID}", agent_type="doc_planner"
    )
    generator_config = AgentConfig(
        agent_id=f"doc_generator_agent_{_USER_ID}", agent_type="doc_generator"
    )

    planner_ctx = AgentExecutionContext(
        agent_type="doc_planner",
        provider=mock_llm_planner,
        model_name="gemini-test",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
    )
    generator_ctx = AgentExecutionContext(
        agent_type="doc_generator",
        provider=mock_llm_generator,
        model_name="claude-test",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
    )

    planner = DocPlannerAgent(
        config=planner_config,
        execution_context=planner_ctx,
        coordinator=coordinator,
        prompt_builder=mock_prompt_builder,
        user_id=_USER_ID,
    )
    generator = DocGeneratorAgent(
        config=generator_config,
        execution_context=generator_ctx,
        docx_runner=mock_docx_runner,
        prompt_builder=mock_prompt_builder,
        user_id=_USER_ID,
    )

    coordinator.register_agent(planner)
    coordinator.register_agent(generator)

    worker = AgentWorkerHandler(
        coordinator=coordinator,
        notification_service=mock_notification,
    )

    return SimpleNamespace(
        coordinator=coordinator,
        registry=registry,
        planner=planner,
        generator=generator,
        worker=worker,
        mock_llm_planner=mock_llm_planner,
        mock_llm_generator=mock_llm_generator,
        mock_docx_runner=mock_docx_runner,
        mock_notification=mock_notification,
        mock_task_queue=mock_task_queue,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _worker_payload(query: str = "Create a quarterly sales report") -> dict:
    return {
        "agent_id": "doc_planner_agent",
        "intent": Intent.CREATE_DOCUMENT,
        "query": query,
        "context": _CONTEXT,
    }


def _delegate_message(query: str = "Create a quarterly sales report") -> AgentMessage:
    return AgentMessage(
        intent=AgentIntent.DELEGATE,
        payload={"query": query, "intent": Intent.CREATE_DOCUMENT},
        sender="worker",
        recipient=f"doc_planner_agent_{_USER_ID}",
        task_id="task_integration",
        context=_CONTEXT,
    )


# ---------------------------------------------------------------------------
# ASYNC dispatch
# ---------------------------------------------------------------------------


class TestAsyncDispatch:

    async def test_handle_delegation_enqueues_task(self, pipeline):
        response = await pipeline.coordinator.handle_delegation(
            intent=Intent.CREATE_DOCUMENT,
            query="Create a report",
            context=_CONTEXT,
        )
        assert response.status == AgentStatus.SUCCESS
        pipeline.mock_task_queue.enqueue_agent_task.assert_called_once()

    async def test_enqueued_task_has_correct_agent_id(self, pipeline):
        await pipeline.coordinator.handle_delegation(
            intent=Intent.CREATE_DOCUMENT,
            query="Create a report",
            context=_CONTEXT,
        )
        call_kwargs = pipeline.mock_task_queue.enqueue_agent_task.call_args.kwargs
        assert call_kwargs["agent_id"] == "doc_planner_agent"

    async def test_enqueued_task_has_correct_intent(self, pipeline):
        await pipeline.coordinator.handle_delegation(
            intent=Intent.CREATE_DOCUMENT,
            query="Create a report",
            context=_CONTEXT,
        )
        call_kwargs = pipeline.mock_task_queue.enqueue_agent_task.call_args.kwargs
        assert call_kwargs["intent"] == Intent.CREATE_DOCUMENT

    async def test_enqueued_task_carries_query(self, pipeline):
        query = "Make a detailed Q1 report"
        await pipeline.coordinator.handle_delegation(
            intent=Intent.CREATE_DOCUMENT,
            query=query,
            context=_CONTEXT,
        )
        call_kwargs = pipeline.mock_task_queue.enqueue_agent_task.call_args.kwargs
        assert call_kwargs["query"] == query

    async def test_enqueued_task_ack_is_success(self, pipeline):
        response = await pipeline.coordinator.handle_delegation(
            intent=Intent.CREATE_DOCUMENT,
            query="Create a report",
            context=_CONTEXT,
        )
        assert response.status == AgentStatus.SUCCESS
        assert response.result["status"] == "started"

    async def test_generator_intent_is_sync_not_enqueued(self, pipeline):
        await pipeline.coordinator.handle_delegation(
            intent=Intent.GENERATE_DOCX_CODE,
            query=json.dumps(_VALID_SPEC),
            context=_CONTEXT,
        )
        pipeline.mock_task_queue.enqueue_agent_task.assert_not_called()


# ---------------------------------------------------------------------------
# Worker Handler → DocPlanner → DocGenerator (happy path)
# ---------------------------------------------------------------------------


class TestWorkerHandlerFullPipeline:

    async def test_handle_task_returns_success(self, pipeline):
        result = await pipeline.worker.handle_task(_worker_payload())
        assert result["status"] == "success"

    async def test_handle_task_calls_notify_file_bytes(self, pipeline):
        await pipeline.worker.handle_task(_worker_payload())
        pipeline.mock_notification.notify_file_bytes.assert_called_once()

    async def test_delivered_file_bytes_match_generator_output(self, pipeline):
        await pipeline.worker.handle_task(_worker_payload())
        kwargs = pipeline.mock_notification.notify_file_bytes.call_args.kwargs
        assert kwargs["file_bytes"] == _FAKE_DOCX_BYTES

    async def test_delivered_filename_ends_with_docx(self, pipeline):
        await pipeline.worker.handle_task(_worker_payload())
        kwargs = pipeline.mock_notification.notify_file_bytes.call_args.kwargs
        assert kwargs["filename"].endswith(".docx")

    async def test_delivered_user_id_from_context(self, pipeline):
        await pipeline.worker.handle_task(_worker_payload())
        kwargs = pipeline.mock_notification.notify_file_bytes.call_args.kwargs
        assert kwargs["user_id"] == _USER_ID

    async def test_handle_task_result_contains_agent_id(self, pipeline):
        result = await pipeline.worker.handle_task(_worker_payload())
        assert f"doc_planner_agent_{_USER_ID}" in result["agent_id"]


# ---------------------------------------------------------------------------
# DocPlanner → DocGenerator SYNC coordination
# ---------------------------------------------------------------------------


class TestPlannerGeneratorDelegation:

    async def test_planner_response_has_delivery_items(self, pipeline):
        response = await pipeline.coordinator.route_message(_delegate_message())
        assert response.status == AgentStatus.SUCCESS
        assert len(response.delivery_items) == 1

    async def test_delivery_item_type_is_file_upload(self, pipeline):
        response = await pipeline.coordinator.route_message(_delegate_message())
        assert response.delivery_items[0].type == "file_upload"

    async def test_delivery_item_contains_base64_bytes(self, pipeline):
        response = await pipeline.coordinator.route_message(_delegate_message())
        b64 = response.delivery_items[0].data["file_bytes_b64"]
        assert base64.b64decode(b64) == _FAKE_DOCX_BYTES

    async def test_result_contains_task_summary(self, pipeline):
        response = await pipeline.coordinator.route_message(_delegate_message())
        assert "Quarterly sales report" in response.result

    async def test_generator_llm_called_exactly_once_on_success(self, pipeline):
        await pipeline.coordinator.route_message(_delegate_message())
        pipeline.mock_llm_generator.generate_content.assert_called_once()

    async def test_docx_runner_called_with_js_code(self, pipeline):
        await pipeline.coordinator.route_message(_delegate_message())
        pipeline.mock_docx_runner.run.assert_called_once()
        call_args = pipeline.mock_docx_runner.run.call_args
        js_code = call_args.args[0] if call_args.args else call_args.kwargs.get("js_code")
        assert "console.log" in js_code


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


class TestRetryBehavior:

    async def test_json_parse_error_retried_then_succeeds(self, pipeline):
        pipeline.mock_llm_planner.generate_content.side_effect = [
            LLMResponse(text="not valid json {{{", tool_calls=[]),
            LLMResponse(text=json.dumps(_VALID_SPEC), tool_calls=[]),
        ]
        response = await pipeline.coordinator.route_message(_delegate_message())
        assert response.status == AgentStatus.SUCCESS
        assert pipeline.mock_llm_planner.generate_content.call_count == 2

    async def test_generator_failure_retried_max_times(self, pipeline):
        pipeline.mock_llm_generator.generate_content.return_value = LLMResponse(
            text="I cannot do this.", tool_calls=[]
        )
        response = await pipeline.coordinator.route_message(_delegate_message())
        assert response.status == AgentStatus.FAILED
        assert pipeline.mock_llm_generator.generate_content.call_count == DocPlannerAgent.MAX_RETRIES

    async def test_generator_failure_planner_llm_retried_max_times(self, pipeline):
        pipeline.mock_llm_generator.generate_content.return_value = LLMResponse(
            text="I cannot do this.", tool_calls=[]
        )
        await pipeline.coordinator.route_message(_delegate_message())
        assert pipeline.mock_llm_planner.generate_content.call_count == DocPlannerAgent.MAX_RETRIES

    async def test_runner_error_triggers_llm_retry(self, pipeline):
        """DocxRunnerPort raises DocxRunnerError → LLM gets error feedback → retries."""
        pipeline.mock_docx_runner.run.side_effect = [
            DocxRunnerError("node not found"),
            _FAKE_DOCX_BYTES,
        ]
        pipeline.mock_llm_generator.generate_content.side_effect = [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(name="generate_docx", args={"js_code": "bad_script()"})],
            ),
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(name="generate_docx", args={"js_code": "good_script()"})],
            ),
        ]
        response = await pipeline.coordinator.route_message(_delegate_message())
        assert response.status == AgentStatus.SUCCESS
        assert pipeline.mock_llm_generator.generate_content.call_count == 2

    async def test_generator_success_on_second_attempt(self, pipeline):
        pipeline.mock_llm_generator.generate_content.side_effect = [
            LLMResponse(text="I cannot do this.", tool_calls=[]),
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(name="generate_docx", args={"js_code": "ok"})],
            ),
        ]
        response = await pipeline.coordinator.route_message(_delegate_message())
        assert response.status == AgentStatus.SUCCESS
        assert pipeline.mock_llm_generator.generate_content.call_count == 2


# ---------------------------------------------------------------------------
# Failure delivery
# ---------------------------------------------------------------------------


class TestFailureDelivery:

    async def test_all_retries_exhausted_returns_failed_status(self, pipeline):
        pipeline.mock_llm_generator.generate_content.return_value = LLMResponse(
            text="cannot", tool_calls=[]
        )
        result = await pipeline.worker.handle_task(_worker_payload())
        assert result["status"] == "failed"

    async def test_all_retries_exhausted_calls_notify(self, pipeline):
        pipeline.mock_llm_generator.generate_content.return_value = LLMResponse(
            text="cannot", tool_calls=[]
        )
        await pipeline.worker.handle_task(_worker_payload())
        pipeline.mock_notification.notify.assert_called_once()

    async def test_failure_notification_mentions_document(self, pipeline):
        pipeline.mock_llm_generator.generate_content.return_value = LLMResponse(
            text="cannot", tool_calls=[]
        )
        await pipeline.worker.handle_task(_worker_payload())
        kwargs = pipeline.mock_notification.notify.call_args.kwargs
        assert "Document" in kwargs["system_alert"] or "document" in kwargs["system_alert"]

    async def test_failure_notification_goes_to_correct_user(self, pipeline):
        pipeline.mock_llm_generator.generate_content.return_value = LLMResponse(
            text="cannot", tool_calls=[]
        )
        await pipeline.worker.handle_task(_worker_payload())
        kwargs = pipeline.mock_notification.notify.call_args.kwargs
        assert kwargs["user_id"] == _USER_ID

    async def test_non_ready_spec_does_not_call_notify_file_bytes(self, pipeline):
        pipeline.mock_llm_planner.generate_content.return_value = LLMResponse(
            text=json.dumps({"status": "clarification_needed", "task_summary": "Need info"}),
            tool_calls=[],
        )
        await pipeline.worker.handle_task(_worker_payload())
        pipeline.mock_notification.notify_file_bytes.assert_not_called()

    async def test_non_ready_spec_triggers_docx_failure_notification(self, pipeline):
        pipeline.mock_llm_planner.generate_content.return_value = LLMResponse(
            text=json.dumps({"status": "clarification_needed", "task_summary": "Need info"}),
            tool_calls=[],
        )
        await pipeline.worker.handle_task(_worker_payload())
        pipeline.mock_notification.notify.assert_called_once()


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


class TestRegistryConstraints:

    def test_doc_planner_intent_is_available(self, pipeline):
        intents = [i["name"] for i in pipeline.registry.get_available_intents()]
        assert Intent.CREATE_DOCUMENT in intents

    def test_doc_generator_intent_is_not_available(self, pipeline):
        intents = [i["name"] for i in pipeline.registry.get_available_intents()]
        assert Intent.GENERATE_DOCX_CODE not in intents
