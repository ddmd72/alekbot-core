"""Facts S4 · T11b: the Cabinet page's answer buttons (a static check of the page source): the buttons' labels come from the
server (the question view's `options`), never from strings in the page; every result the route can answer has its text in
en/uk/fr/es; a closed fact has no button, and no server id lands in an inline handler or unescaped."""
import re
from pathlib import Path

import pytest

from src.domain.fact_answer_result import AnswerResult

_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"
LANGS = ("en", "uk", "fr", "es")
RESULTS = [r.value for r in AnswerResult] + ["error"]
STRINGS = ["questions.mark_selected", "questions.select", "questions.none_selected", "questions.failed"]


def _page() -> str:
    return _HTML.read_text(encoding="utf-8")


def _dicts():
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
def test_every_result_and_button_string_exists_in_every_language(lang):
    keys = STRINGS + [f"questions.result_{r}" for r in RESULTS]
    assert [k for k in keys if not _dicts()[lang].get(k)] == []


def test_result_texts_are_not_copies_of_english():
    dicts = _dicts()
    keys = [f"questions.result_{r}" for r in RESULTS] + STRINGS
    assert [(lang, k) for lang in ("uk", "fr", "es") for k in keys if dicts[lang][k] == dicts["en"][k]] == []


def test_the_answer_labels_come_from_the_server_not_from_the_page():
    # P2 revised (C15): the buttons are built from the options the server sends with each question, not from one
    # page-wide list: answerButtons takes them as its argument, a row passes its question's, a bulk bar its group's
    body = _function("answerButtons")
    assert body.strip().startswith("function answerButtons(options, questionId)")
    assert "return options" in body and ".map(" in body
    assert "${escapeHtml(o.label)}" in body and "${escapeHtml(o.answer)}" in body
    assert "answerButtons(q.options, q.question_id)" in _page()
    assert "data.options" not in _page() and "reviewOptions" not in _page()
    for label in ("Yes", "No longer", "Never"):
        assert f">{label}<" not in _page()


def test_the_page_posts_the_answers_the_route_takes():
    body = _function("sendReviewAnswers")
    assert "/api/user/facts/review-answers" in body and 'method: "POST"' in body
    assert "JSON.stringify({ answers })" in body


def test_a_changed_result_reloads_the_lists():
    body = _function("sendReviewAnswers")
    assert 'r.result === "changed"' in body and "await loadReviewQuestions()" in body


def test_a_closed_fact_has_no_button():
    body = _function("closedLoad")
    assert "<button" not in body and "restore" not in body.lower()


def test_ids_reach_the_dom_escaped_and_never_an_inline_handler():
    page = _page()
    assert 'data-question-id="${escapeHtml(q.question_id)}"' in page
    assert "${q.question_id}" not in page
    assert [h for h in re.findall(r'on[a-z]+="([^"]*)"', page) if "question" in h or "${o." in h] == []
