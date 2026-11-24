"""The production graph wires the Slack «Send» button of a held external task (EXTERNAL_TASK_QUEUE_RFC §5.3): the worker's
one click service answers `external_task_send` through the graph's one external task service, in the users' UI
language, and both chat handlers hold the click service's own interactive-message router for posting the button (the
Telegram handler holds it too, and posts no button: Slack only)."""
import pytest

from src.domain.external_task_receipt import EXTERNAL_TASK_SEND_KIND
from src.domain.fact_review_question import FACT_QUESTION_KIND
from src.services.external_task_send_choice import ExternalTaskSendChoice
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow


async def test_the_click_service_answers_the_send_button_through_the_graphs_external_task_service(prod_graph: ProdGraph):
    app = prod_graph.app
    service = app.worker_handler._interactive_choice
    send = service._handlers[EXTERNAL_TASK_SEND_KIND]

    assert isinstance(send, ExternalTaskSendChoice) and send.kind == EXTERNAL_TASK_SEND_KIND
    assert app.external_tasks is not None and send._tasks is app.external_tasks
    assert service._handlers[FACT_QUESTION_KIND] is app.worker_handler._fact_review._answer_service
    assert send._language is app.language_service and send._localization is not None


async def test_the_chat_handlers_post_the_button_through_the_click_services_router(prod_graph: ProdGraph):
    app = prod_graph.app
    router = app.worker_handler._interactive_choice._interactive

    for adapter in (app.slack_adapter, app.telegram_adapter):
        assert adapter.conversation_handler._task_send_buttons is router
