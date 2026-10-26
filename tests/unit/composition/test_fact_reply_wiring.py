"""Facts S4 · T8f: both chat adapters' conversation handlers hold the one `FactAnswerService` the memory review uses, so a
reply to a question's message is taken at intake on Slack and on Telegram (plan §5 T8f, §3.16)."""
import pytest

from src.services.fact_answer_service import FactAnswerService
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow


async def test_the_slack_and_telegram_handlers_hold_the_reviews_answer_service(prod_graph: ProdGraph):
    app = prod_graph.app
    answers = app.worker_handler._fact_review._answer_service

    assert isinstance(answers, FactAnswerService)
    assert app.slack_adapter.conversation_handler._fact_answers is answers
    assert app.telegram_adapter.conversation_handler._fact_answers is answers
