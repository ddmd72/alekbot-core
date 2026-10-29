"""Facts S2 · T7: one ConsolidationService, built in the composition root, serves `$consolidate` on Slack and Telegram.

The composition root is `src/composition/application_graph.py` (`build_application_graph`) since P9
(`decisions/application_graph_builder.md`); `main.py` calls it and constructs no services itself.
"""
import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition import slack_adapter_factory, telegram_adapter_factory
from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.composition.telegram_adapter_factory import TelegramAdapterFactory
from src.domain.messaging import MessageContext
from src.handlers.conversation_handler import ConversationHandler
from src.services.consolidation_service import ConsolidationService

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
_GRAPH = _SRC / "composition" / "application_graph.py"


class _Captured(Exception):
    """Raised by the recording handler stand-in so the factory stops once it has the handler's arguments."""

    def __init__(self, kwargs):
        self.kwargs = kwargs


def _recording_handler(*_args, **kwargs):
    raise _Captured(kwargs)


def _service():
    return ConsolidationService(
        queue=MagicMock(), coordinator=MagicMock(), agent_factory=MagicMock(), lease=MagicMock(), run_timeout_s=900.0,
        user_repo=MagicMock(), owner_zones=MagicMock(),
    )


def _handler_kwargs_from_slack(monkeypatch, service):
    monkeypatch.setattr(slack_adapter_factory, "ConversationHandler", _recording_handler)
    with pytest.raises(_Captured) as caught:
        SlackAdapterFactory.create_adapter(
            rich_content_service=MagicMock(),
            app=MagicMock(), config={}, env_config=MagicMock(), coordinator=MagicMock(), agent_factory=MagicMock(),
            iam_service=MagicMock(), file_service=MagicMock(), session_store=MagicMock(), db_client=MagicMock(),
            consolidation_service=service,
        )
    return caught.value.kwargs


def _handler_kwargs_from_telegram(monkeypatch, service):
    monkeypatch.setattr(telegram_adapter_factory, "ConversationHandler", _recording_handler)
    with pytest.raises(_Captured) as caught:
        TelegramAdapterFactory.create_adapter(
            rich_content_service=MagicMock(),
            token="1:abc", webhook_secret="s", dedup_store=MagicMock(), session_store=MagicMock(),
            coordinator=MagicMock(), agent_factory=MagicMock(), iam_service=MagicMock(), file_service=MagicMock(),
            consolidation_service=service,
        )
    return caught.value.kwargs


@pytest.mark.parametrize("build", [_handler_kwargs_from_slack, _handler_kwargs_from_telegram])
def test_the_factories_hand_over_the_shared_services_consolidate_now(monkeypatch, build):
    service = _service()

    kwargs = build(monkeypatch, service)

    assert kwargs["overflow_callback"] == service.consolidate_now


@pytest.mark.parametrize("build", [_handler_kwargs_from_slack, _handler_kwargs_from_telegram])
def test_the_factories_pass_none_without_a_service(monkeypatch, build):
    assert build(monkeypatch, None)["overflow_callback"] is None


async def test_without_a_service_the_consolidate_command_still_only_enqueues():
    queue = MagicMock()
    queue.enqueue_batch = AsyncMock(return_value="b1")
    session = MagicMock(user_id="u1", session_id="s1", messages=[MagicMock()])
    message = MagicMock(role="user", created_at="2026-01-01T00:00:00")
    message.parts = [MagicMock(text="hello", consolidation_text=None)]
    session.extract_oldest_messages = MagicMock(return_value=[message])
    store = MagicMock(load_session=AsyncMock(return_value=session), save_session=AsyncMock())
    agent_factory = MagicMock(get_session_store=MagicMock(return_value=store), ensure_agents_for_user=AsyncMock())
    handler = ConversationHandler(
        coordinator=MagicMock(), agent_factory=agent_factory, file_service=MagicMock(),
        consolidation_queue=queue, overflow_callback=None,
    )
    channel = MagicMock(send_message=AsyncMock(), send_status=AsyncMock())

    await handler.handle_command("consolidate", MessageContext(text="$consolidate", session_id="s1", user_id="u1",
                                                               account_id="a1"), channel)

    queue.enqueue_batch.assert_awaited_once()
    assert "background" in channel.send_message.await_args.args[0]


def test_the_one_consolidation_service_is_constructed_only_in_the_application_graph():
    constructed = []
    sources = [*_SRC.rglob("*.py"), _ROOT / "main.py", *(p for p in _ROOT.glob("*_main.py"))]
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "ConsolidationService":
                    constructed.append(str(path.relative_to(_ROOT)))
    assert constructed == ["src/composition/application_graph.py"]


def _calls(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)]


def test_the_graph_builds_the_one_service_with_the_lease_the_queue_and_the_run_timeout():
    tree = ast.parse(_GRAPH.read_text(encoding="utf-8"))

    (built,) = _calls(tree, "ConsolidationService")
    keywords = {k.arg: ast.unparse(k.value) for k in built.keywords}

    # The one lease (Facts S4 T10a): built once under one name, which both writers of an account's memory receive.
    (lease_built,) = _calls(tree, "FirestoreMemoryWriteLease")
    (lease_assign,) = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and n.value is lease_built]
    assert [ast.unparse(t) for t in lease_assign.targets] == ["_memory_write_lease"]
    (review,) = _calls(tree, "FactReviewService")
    assert keywords["lease"] == "_memory_write_lease"
    assert {k.arg: ast.unparse(k.value) for k in review.keywords}["lease"] == "_memory_write_lease"
    assert keywords["task_queue"] == "agent_task_queue"
    assert keywords["run_timeout_s"] == "CONSOLIDATION_CFG.timeout_ms / 1000"


@pytest.mark.parametrize("factory", ["SlackAdapterFactory", "TelegramAdapterFactory"])
def test_the_graph_passes_the_service_to_both_adapter_factories(factory):
    tree = ast.parse(_GRAPH.read_text(encoding="utf-8"))

    (call,) = [c for c in _calls(tree, "create_adapter") if getattr(c.func.value, "id", None) == factory]

    assert {k.arg: ast.unparse(k.value) for k in call.keywords}["consolidation_service"] == "_consolidation_service"
