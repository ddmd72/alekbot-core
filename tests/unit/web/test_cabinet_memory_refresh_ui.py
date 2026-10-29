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


def test_a_section_entrance_plays_once_per_visit_history():
    body = _function("navigate")
    assert "_visitedSections.has(name)" in body and "_visitedSections.add(name)" in body
    # the replay lives inside the first-visit branch only
    assert body.index("_visitedSections.has(name)") < body.index('classList.remove("visible")')


def test_answered_rows_leave_locally_without_a_list_reload():
    answers = _function("sendReviewAnswers")
    assert "removeReviewRows(done)" in answers
    # only a result other than "applied" falls back to the server list
    assert answers.index("removeReviewRows(done)") < answers.index("await loadReviewQuestions();")
    body = _function("removeReviewRows")
    assert "prefers-reduced-motion: reduce" in body
    assert "CSS.escape(id)" in body
    assert "innerHTML = html" not in body


def test_a_facts_refresh_that_finds_nothing_new_leaves_the_cards_alone():
    body = _function("browseLoad")
    assert "keepList &&" in body and "JSON.stringify(allFacts.slice(0, newFacts.length))" in body


def test_long_question_lists_show_a_page_and_a_show_more_button():
    page = _HTML.read_text(encoding="utf-8")
    assert "const REVIEW_PAGE = 10;" in page
    assert "reviewQuestions.filter((q) => q.group === group);\n        if (qs.length === 0) return \"\";\n        const shown = qs.slice(0, reviewShown[group]);" in page
    assert "data-show-more" in _function("onReviewClick")
    assert "reviewShown[group] += REVIEW_PAGE" in _function("showMoreReview")


def test_show_more_has_a_text_in_every_language():
    page = _HTML.read_text(encoding="utf-8")
    assert len(re.findall(r'"questions\.show_more": "[^"]+"', page)) == 4
