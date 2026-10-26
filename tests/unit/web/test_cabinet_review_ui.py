"""Facts S4 · T11a: the Cabinet page's memory views (a static check of the page source): every string they use exists in
en/uk/fr/es, and what the server sends reaches the DOM escaped, never inside an inline handler."""
import re
from pathlib import Path

import pytest

_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"
LANGS = ("en", "uk", "fr", "es")
PREFIXES = ("memory.state_", "memory.closed_", "questions.")


def _page() -> str:
    return _HTML.read_text(encoding="utf-8")


def _dicts():
    body = re.search(r"const LANG_DICT = \{(.*?)\n      \};", _page(), re.S).group(1)
    out = {}
    for lang in LANGS:
        block = re.search(rf"\n        {lang}: \{{(.*?)\n        \}},", body, re.S).group(1)
        out[lang] = dict(re.findall(r'^\s*"([\w.]+)": "((?:[^"\\]|\\.)*)",?$', block, re.M))
    return out


def _used() -> set:
    return set(re.findall(r'\bt\(\s*"((?:memory\.state_|memory\.closed_|questions\.)[\w.]*)"\s*\)', _page())) | set(
        re.findall(r'"((?:memory\.state_|memory\.closed_)\w+)"', _page()))


def test_the_page_uses_the_review_strings():
    assert {"questions.title", "memory.closed_empty", "questions.status_sent"} <= _used()


@pytest.mark.parametrize("lang", LANGS)
def test_every_string_the_page_uses_exists_in_every_language(lang):
    missing = sorted(key for key in _used() if not _dicts()[lang].get(key))
    assert missing == []


def test_translations_are_not_copies_of_english():
    dicts = _dicts()
    keys = [k for k in dicts["en"] if k.startswith(PREFIXES)]
    assert keys and [(lang, k) for lang in ("uk", "fr", "es") for k in keys
                     if dicts[lang][k] == dicts["en"][k] and k != "questions.title"] == []


def test_a_question_and_a_closed_fact_are_set_escaped():
    page = _page()
    assert "${escapeHtml(q.text)}" in page and "${escapeHtml(f.text)}" in page
    assert "${q.text}" not in page and "${f.text}" not in page and "${q.question_id}" not in page


def test_no_server_id_lands_inside_an_inline_handler():
    handlers = re.findall(r'on[a-z]+="([^"]*)"', _page())
    assert [h for h in handlers if "${q." in h or "${f.id" in h] == []
