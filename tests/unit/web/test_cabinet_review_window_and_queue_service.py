"""The Cabinet's memory-review routes after T12d: the set-aside window comes from the API, and the review queue is read
through `FactAnswerService.review_queue` (a route with no answer service is a 501)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_overview import SET_ASIDE_WINDOW, QueueEntry
from src.web.user_cabinet_app import create_user_cabinet_blueprint

TOKEN = {"Authorization": "Bearer t"}
ASKED = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


def _app(**kwargs) -> Quart:
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "acc", "role": "owner"})
    app = Quart("test_cabinet_review_window_and_queue_service")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(),
        embedding_service=MagicMock(), **{"fact_repo": MagicMock(), **kwargs},
    ))
    return app


async def test_the_recently_closed_list_says_how_many_days_it_covers():
    repo = MagicMock()
    repo.get_recently_closed = AsyncMock(return_value=[])
    async with _app(fact_repo=repo).test_client() as client:
        body = await (await client.get("/api/user/facts/recently-closed", headers=TOKEN)).get_json()
    assert body == {"facts": [], "window_days": SET_ASIDE_WINDOW.days}


async def test_the_review_queue_maps_the_services_entries_and_options_to_json():
    service = MagicMock()
    service.review_queue = AsyncMock(return_value=[
        QueueEntry(question_id="q1", fact_id="f1", text="Drinks green tea", domain="preference", reported_date=None,
                   waiting_since=ASKED, asked_at=None, place="waiting"),
    ])
    # P2 revised (C14): answer_options maps each question kind to its options with their labels
    service.answer_options = AsyncMock(return_value={"still_true": [(AnswerKind.STILL_TRUE, "Yes"), (AnswerKind.ENDED, "No longer")]})
    async with _app(fact_answer_service=service).test_client() as client:
        response = await client.get("/api/user/facts/review-queue", headers=TOKEN)
        body = await response.get_json()
    assert response.status_code == 200
    service.review_queue.assert_awaited_once_with("acc", "u1")
    assert body["questions"] == [{
        "question_id": "q1", "text": "Drinks green tea", "domain": "preference", "reported_date": None,
        "waiting_since": ASKED.isoformat(), "asked_at": None, "status": "waiting",
        # P2 revised (C15): each item carries its kind, its group and its kind's options; no top-level options
        "kind": "still_true", "group": "questions",
        "options": [{"answer": "still_true", "label": "Yes"}, {"answer": "ended", "label": "No longer"}],
    }]
    assert "options" not in body


async def test_without_an_answer_service_the_review_queue_is_not_configured():
    async with _app().test_client() as client:
        response = await client.get("/api/user/facts/review-queue", headers=TOKEN)
    assert response.status_code == 501
