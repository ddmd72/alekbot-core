"""The External workers help page (`/cabinet/help/workers`, `src/web/static/workers_help.html`): a static page that
explains the feature in the Cabinet's four languages. The route serves it revalidated; the four dictionaries hold the
same keys and every key the markup or the script names exists; the script builds DOM nodes and never an HTML string; the
numbers the page states are the domain constants; the Cabinet links to it with its own language and carries the
one-line hints. The Cabinet shell's own tests: `test_cabinet_external_workers_page.py`."""
import re
from pathlib import Path
from unittest.mock import MagicMock

from src.domain import external_task as limits
from tests.unit.web.test_cabinet_external_workers_page import LANGS, _SETTINGS_JS, _SHELL, _dict
from tests.unit.web.test_user_cabinet_app import _app

_PAGE = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "workers_help.html").read_text(encoding="utf-8")
_SCRIPT = _PAGE[_PAGE.index("<script>"):]
_MARKUP = _PAGE[:_PAGE.index("<script>")]


def _page_dict(lang: str) -> dict:
    section = re.search(r"\n        " + lang + r": \{\n(.*?)\n        \},", _PAGE, re.S)
    assert section, lang
    return dict(re.findall(r'^\s{10}"([\w.]+)": ("(?:[^"\\]|\\.)*"),$', section.group(1), re.M))


def _text(lang: str, key: str) -> str:
    import json
    return json.loads(_page_dict(lang)[key])


async def test_the_page_is_served_on_the_cabinet_path_and_revalidated():
    app = _app(email_indexing_service=MagicMock(), email_job_repo=MagicMock(),
               task_queue=MagicMock(), oauth_credentials_port=MagicMock())
    resp = await app.test_client().get("/cabinet/help/workers?lang=uk")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-cache"
    assert await resp.get_data() == _PAGE.encode("utf-8")


def test_the_four_dictionaries_hold_the_same_keys():
    en = set(_page_dict("en"))
    assert len(en) > 80
    for lang in LANGS:
        assert set(_page_dict(lang)) == en, lang


def test_every_key_the_markup_names_exists_and_every_key_is_used():
    en = set(_page_dict("en"))
    used = set(re.findall(r'data-i18n(?:-list)?="([\w.]+)"', _MARKUP)) | set(re.findall(r'\bt\("(\w+\.[\w.]+)"\)', _SCRIPT))
    assert used - en == set()
    assert en - used - {"app.title"} == set()
    assert 't("app.title")' in _SCRIPT


def test_no_dictionary_text_is_ever_set_as_html():
    assert "innerHTML" not in _SCRIPT and "insertAdjacentHTML" not in _SCRIPT and "document.write" not in _SCRIPT
    assert "<script" not in "".join(_page_dict(lang)[k] for lang in LANGS for k in _page_dict(lang))


def test_the_language_comes_from_the_query_then_the_phone_then_english():
    body = _SCRIPT[_SCRIPT.index("function pickLanguage"):_SCRIPT.index("const LANG = ")]
    assert body.index('get("lang")') < body.index("navigator.language") < body.index('"en"')


def test_the_page_has_light_and_dark_tokens_and_a_phone_viewport():
    assert "prefers-color-scheme: dark" in _MARKUP and "width=device-width" in _MARKUP


def test_the_numbers_the_page_states_are_the_domain_constants():
    en = _text("en", "flow.2.d") + _text("en", "flow.4.d") + _text("en", "note.claims") + _text("en", "note.expiry")
    en += _text("en", "wake.p4") + _text("en", "connect.a.d")
    assert f"{limits.HOLD_S // 3600} hours" in en
    assert f"{limits.LEASE_S // 60} minutes" in en
    assert f"Taken {limits.MAX_CLAIMS_PER_TASK} times" in en
    assert f"within {limits.TASK_MAX_AGE_S // 86_400} days" in en
    assert f"up to {limits.WAKE_MAX_ATTEMPTS} times" in en
    assert f"up to {limits.MAX_KEYS_PER_WORKER} keys" in en
    stated = {"flow.2.d": limits.HOLD_S // 3600, "flow.4.d": limits.LEASE_S // 60, "note.claims": limits.MAX_CLAIMS_PER_TASK,
              "note.expiry": limits.TASK_MAX_AGE_S // 86_400, "wake.p4": limits.WAKE_MAX_ATTEMPTS,
              "connect.a.d": limits.MAX_KEYS_PER_WORKER}
    for lang in LANGS:
        for key, number in stated.items():
            assert re.search(rf"(?<!\d){number}(?!\d)", _text(lang, key)), (lang, key, number)


def test_the_page_uses_only_the_invented_examples():
    for lang in LANGS:
        assert "Shelbyville" in _text(lang, "flow.1.d") or "Шелбівіля" in _text(lang, "flow.1.d")
    assert "`research`" in _text("en", "what.b1") and "`calendar-watch`" in _text("en", "what.b1")


def test_the_cabinet_links_to_the_page_with_its_own_language_and_every_hint_is_in_four_languages():
    assert 'id="workers-help-link"' in _SHELL and 'href="/cabinet/help/workers"' in _SHELL
    assert '"#workers-help-link": "workers.help_link"' in _SETTINGS_JS
    assert "/cabinet/help/workers?lang=${encodeURIComponent(_currentLang)}" in _SETTINGS_JS
    for key in ("hint_keys", "hint_wake_curl", "hint_wake_secret", "hint_grants"):
        assert f't("workers.{key}")' in _SETTINGS_JS, key
    for key in ("help_link", "hint_keys", "hint_wake_curl", "hint_wake_secret", "hint_grants"):
        for lang in LANGS:
            assert _dict(lang).get(f"workers.{key}"), (lang, key)
