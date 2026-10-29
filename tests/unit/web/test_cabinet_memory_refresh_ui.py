"""The Memory page refreshes in place (a static check of the page source): answering review questions neither flashes the
skeleton nor drops the owner's ticked rows, and "Load more" appends instead of rebuilding every card."""
import re
from pathlib import Path

_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"


def _function(name: str) -> str:
    match = re.search(rf"\n(\s*(?:async )?function {name}\(.*?\)\s*\{{.*?\n      \}})\n", _HTML.read_text(encoding="utf-8"), re.S)
    assert match, name
    return match.group(1)


def test_an_answer_refreshes_the_facts_without_the_skeleton():
    assert "browseLoad(true, true)" in _function("sendReviewAnswers")
    assert "isFirst && !keepList" in _function("browseLoad")
    assert "renderFacts(newFacts, isFirst, keepList)" in _function("browseLoad")


def test_refreshed_cards_are_visible_at_once():
    body = _function("renderFacts")
    assert 'instant ? " visible" : ""' in body


def test_load_more_appends_without_rebuilding_the_cards():
    body = _function("renderFacts")
    assert 'insertAdjacentHTML("beforeend"' in body
    assert "innerHTML += " not in body


def test_the_question_list_repaints_only_on_change_and_keeps_ticked_rows():
    body = _function("renderReviewQuestions")
    assert "box.dataset.rendered === html" in body
    assert "ticked.has(c.dataset.questionId)" in body
