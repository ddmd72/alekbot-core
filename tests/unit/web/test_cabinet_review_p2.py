"""Facts P2 · card D: the Cabinet's review routes and page by question kind. The queue view gives each question its kind,
its group and its own options, and no top-level options; the answers route reads each answer against its question's
options (a missing question still reaches `apply` and is told `not_found`); the page splits invoices from questions, keeps
one bulk bar per group and escapes every id. The answer service is a small stub with the card's `answer_options`."""
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_options import OPTIONS
from src.domain.fact_review_overview import QueueEntry
from src.domain.fact_review_question import TEXT_KEYS, FactReviewQuestion
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry

TOKEN = {"Authorization": "Bearer t"}
ACC, USER, OTHER = "acc-1", "user-1", "user-2"
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"
LANGS = ("en", "uk", "fr", "es")
KEYS = ("questions.invoices_title", "questions.one_kind")


def _entry(qid: str, kind: str = "still_true", delivery: str = "chat") -> QueueEntry:
    return QueueEntry(question_id=qid, fact_id=f"f-{qid}", text="a concert on Friday", domain=None, reported_date=None,
                      waiting_since=T0, asked_at=None, place="waiting", question=kind, delivery=delivery)  # type: ignore[arg-type]


def _question(qid: str, kind: str, user: str = USER) -> FactReviewQuestion:
    texts = {k: f"{k} text" for k in TEXT_KEYS.get((kind, "chat"), ())}  # type: ignore[call-overload]
    return FactReviewQuestion(texts=texts, question_id=qid, account_id=ACC, user_id=user, fact_id=f"f-{qid}", quoted_text="a concert",
                              queue_key=T0, queued_at=T0, fallback="revive" if kind == "still_true" else "history", question=kind)  # type: ignore[arg-type]


class StubService:
    def __init__(self, entries: List[QueueEntry] = ()) -> None:
        self.entries = list(entries)
        self.applied: List[Any] = []

    async def review_queue(self, account_id: str, user_id: str) -> List[QueueEntry]:
        return self.entries

    async def answer_options(self, user_id: str, kinds: Set[str]) -> Dict[str, list]:
        return {k: [(a, f"label:{a.value}") for a in OPTIONS[k] if a is not AnswerKind.DONT_KNOW] for k in kinds}

    async def apply(self, account_id, user_id, question_id, kind, source, now) -> AnswerResult:
        self.applied.append((question_id, kind))
        return AnswerResult.NOT_FOUND if question_id == "missing" else AnswerResult.APPLIED


class StubStore:
    def __init__(self, *questions: FactReviewQuestion) -> None:
        self.by_id = {q.question_id: q for q in questions}

    async def get_questions(self, ids):
        return [self.by_id[i] for i in ids if i in self.by_id]


def _app(service: StubService, store: StubStore) -> Quart:
    sessions = MagicMock()
    sessions.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": ACC, "role": "owner"})
    app = Quart("test_cabinet_review_p2")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=sessions, user_repo=MagicMock(), fact_repo=MagicMock(),
        embedding_service=MagicMock(), fact_review_store=store, fact_answer_service=service, connectors=ConnectorRegistry(),
    ))
    return app


async def _get_queue(service: StubService):
    async with _app(service, StubStore()).test_client() as c:
        resp = await c.get("/api/user/facts/review-queue", headers=TOKEN)
        return resp.status_code, await resp.get_json()


async def _post(service: StubService, store: StubStore, *pairs):
    async with _app(service, store).test_client() as c:
        resp = await c.post("/api/user/facts/review-answers", headers=TOKEN,
                            json={"answers": [{"question_id": q, "answer": a} for q, a in pairs]})
        return resp.status_code, await resp.get_json()


# ------------------------------------------------------------------------------------------------ the queue view

async def test_each_item_carries_its_kind_group_and_options_and_there_is_no_top_level_options():
    service = StubService([_entry("a"), _entry("b", "happened"), _entry("c", "paid", "digest")])

    status, body = await _get_queue(service)

    assert status == 200 and set(body) == {"questions"}
    by_id = {q["question_id"]: q for q in body["questions"]}
    assert (by_id["a"]["kind"], by_id["a"]["group"]) == ("still_true", "questions")
    assert [o["answer"] for o in by_id["a"]["options"]] == ["still_true", "ended", "never"]
    assert (by_id["b"]["kind"], by_id["b"]["group"]) == ("happened", "questions")
    assert by_id["b"]["options"] == [{"answer": "happened", "label": "label:happened"},
                                     {"answer": "not_happened", "label": "label:not_happened"}]
    assert (by_id["c"]["kind"], by_id["c"]["group"]) == ("paid", "invoices")
    assert [o["answer"] for o in by_id["c"]["options"]] == ["paid", "not_paid"]
    assert {"waiting_since", "status", "text"} <= set(by_id["a"])


async def test_an_empty_queue_is_an_empty_list():
    assert await _get_queue(StubService()) == (200, {"questions": []})


# ------------------------------------------------------------------------------------------------ the answers route

async def test_an_answer_of_the_questions_kind_is_applied():
    service = StubService()

    status, body = await _post(service, StubStore(_question("q1", "happened")), ("q1", "happened"))

    assert (status, body) == (200, {"results": [{"question_id": "q1", "result": "applied"}]})
    assert service.applied == [("q1", AnswerKind.HAPPENED)]


async def test_an_answer_of_another_kind_is_a_400_and_nothing_is_applied():
    service = StubService()
    store = StubStore(_question("q1", "happened"), _question("q2", "still_true"))

    status, body = await _post(service, store, ("q2", "still_true"), ("q1", "ended"))

    assert status == 400
    assert body == {"error": "each answer needs a question_id and one of that question's options"}
    assert service.applied == []


async def test_a_missing_question_reads_against_the_union_and_is_not_found():
    service = StubService()

    status, body = await _post(service, StubStore(), ("missing", "paid"))

    assert (status, body) == (200, {"results": [{"question_id": "missing", "result": "not_found"}]})


@pytest.mark.parametrize("item", [None, "x", {"answer": "paid"}, {"question_id": "", "answer": "paid"},
                                  {"question_id": "q1"}, {"question_id": "q1", "answer": 3},
                                  {"question_id": 7, "answer": "paid"}])
async def test_a_malformed_item_is_the_same_400(item):
    service = StubService()
    async with _app(service, StubStore(_question("q1", "paid"))).test_client() as c:
        resp = await c.post("/api/user/facts/review-answers", headers=TOKEN, json={"answers": [item]})
        body = await resp.get_json()

    assert (resp.status_code, body) == (400, {"error": "each answer needs a question_id and one of that question's options"})
    assert service.applied == []


async def test_dont_know_is_no_deciding_answer_of_any_kind():
    service = StubService()
    status, _ = await _post(service, StubStore(_question("q1", "paid")), ("q1", "dont_know"))
    assert status == 400 and service.applied == []


async def test_another_users_question_is_still_a_403_before_any_answer_is_read():
    service = StubService()
    status, body = await _post(service, StubStore(_question("q1", "paid", user=OTHER)), ("q1", "ended"))
    assert (status, body) == (403, {"error": "Not authorized"}) and service.applied == []


# ------------------------------------------------------------------------------------------------ the page

def _page() -> str:
    return HTML.read_text(encoding="utf-8")


def _dicts() -> Dict[str, Dict[str, str]]:
    body = re.search(r"const LANG_DICT = \{(.*?)\n      \};", _page(), re.S).group(1)
    out = {}
    for lang in LANGS:
        block = re.search(rf"\n        {lang}: \{{(.*?)\n        \}},", body, re.S).group(1)
        out[lang] = dict(re.findall(r'^\s*"([\w.]+)": "((?:[^"\\]|\\.)*)",?$', block, re.M))
    return out


def _function(name: str) -> str:
    match = re.search(rf"\n(\s*(?:async )?function {name}\(.*?\)\s*\{{.*?\n      \}})\n", _page(), re.S)
    assert match, name
    return match.group(1)


@pytest.mark.parametrize("lang", LANGS)
def test_the_new_strings_exist_in_every_language(lang):
    assert [k for k in KEYS if not _dicts()[lang].get(k)] == []


def test_the_new_strings_are_not_english_copies():
    dicts = _dicts()
    assert [(lang, k) for lang in ("uk", "fr", "es") for k in KEYS if dicts[lang][k] == dicts["en"][k]] == []


def test_the_invoices_section_is_headed_by_its_own_string():
    assert 't("questions.invoices_title")' in _function("renderReviewQuestions")
    assert 'reviewSection("invoices"' in _function("renderReviewQuestions")
    assert 'reviewSection("questions"' in _function("renderReviewQuestions")


def test_a_bulk_bar_carries_its_group_and_mixed_kinds_show_the_note_and_no_buttons():
    assert 'div data-group="${escapeHtml(group)}"' in _function("reviewSection")
    bar = _function("bulkBarContent")
    assert 't("questions.one_kind")' in bar and "kinds.size > 1" in bar
    assert "answerButtons(first ? first.options" in bar


def test_the_bar_re_renders_on_a_checkbox_change_through_one_listener():
    page = _page()
    assert page.count('box.addEventListener("change", onReviewChange)') == 1
    assert "refreshBulkBar(group)" in _function("onReviewChange")


def test_a_bulk_click_takes_the_ids_of_its_own_group():
    assert "checkedReviewIds(bar.dataset.group)" in _function("onReviewClick")


def test_row_buttons_use_the_items_options_and_ids_are_escaped():
    assert "answerButtons(q.options, q.question_id)" in _function("reviewRow")
    assert "${escapeHtml(o.label)}" in _function("answerButtons")
    assert 'data-question-id="${escapeHtml(questionId)}"' in _function("answerButtons")
    assert "data.options" not in _page()


def test_no_inline_handlers_carry_question_data():
    assert [h for h in re.findall(r'on[a-z]+="([^"]*)"', _page()) if "question" in h or "group" in h] == []
