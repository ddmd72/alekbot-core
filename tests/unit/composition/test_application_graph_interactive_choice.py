"""The production graph wires an owner's button press from the Slack route to the question's answer path (Facts S4 · T8g, T5d).

Each check reads the attribute that holds the value in the object production uses, on the graph built over the deploy's
sealed environment: the route queues on the main task queue with the app's signing secret, and the worker's choice
service resolves users with the same identity service the chat adapter uses, hands `fact_question` choices to the one
answer service the memory review closes questions through, and re-renders through the one interactive-message router."""
import pytest

from src.domain.fact_review_question import FACT_QUESTION_KIND
from src.services.fact_answer_service import FactAnswerService
from src.services.interactive_choice_service import InteractiveChoiceService
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)


@pytest.mark.slow
async def test_the_worker_has_the_choice_service_with_the_graphs_identity_router_and_answer_service(prod_graph: ProdGraph):
    app = prod_graph.app
    service = app.worker_handler._interactive_choice
    review = app.worker_handler._fact_review

    assert isinstance(service, InteractiveChoiceService)
    assert service._iam is app.slack_adapter.iam_service
    assert list(service._handlers) == [FACT_QUESTION_KIND]
    assert isinstance(service._handlers[FACT_QUESTION_KIND], FactAnswerService)
    assert service._handlers[FACT_QUESTION_KIND] is review._answer_service
    assert service._interactive is review._interactive


@pytest.mark.slow
async def test_the_slack_route_queues_on_the_main_task_queue_and_checks_the_apps_signing_secret(prod_graph: ProdGraph):
    app = prod_graph.app
    route = app.slack_adapter._interactivity

    assert route._queue is app.agent_task_queue and route._queue is not None
    assert route._secret == prod_graph.offline.env["SLACK_SIGNING_SECRET"] != ""
    assert route._secret == app.slack_adapter.slack_signing_secret
