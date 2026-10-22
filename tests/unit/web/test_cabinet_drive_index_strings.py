"""Every drive-index string of the Cabinet exists in en/uk/fr/es (USER_DRIVE_INDEXING_RFC §6, §17.7)."""
import re
from pathlib import Path

import pytest

from src.domain.drive_index import ErrorCategory

_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"
LANGS = ("en", "uk", "fr", "es")
NEW_KEYS = (
    "drive.index_title", "drive.index_disclosure", "drive.index_off_confirm", "drive.needs_reconnect",
    "drive.reconnect_hint", "drive.reconnect", "drive.index_searchable", "drive.index_kept_hint",
    "drive.index_pending", "drive.index_failed", "drive.index_building", "drive.index_full",
    "drive.index_last_sync", "drive.index_never", "drive.index_problem", "toast.drive_index_on",
    "toast.drive_index_off", "toast.drive_index_failed",
    *(f"drive.err.{c.value}" for c in ErrorCategory),
)


def _dicts():
    html = _HTML.read_text(encoding="utf-8")
    body = re.search(r"const LANG_DICT = \{(.*?)\n      \};", html, re.S).group(1)
    out = {}
    for lang in LANGS:
        block = re.search(rf"\n        {lang}: \{{(.*?)\n        \}},", body, re.S).group(1)
        out[lang] = dict(re.findall(r'^\s*"([\w.]+)": "((?:[^"\\]|\\.)*)",?$', block, re.M))
    return html, out


def test_parser_reads_existing_keys():
    _, dicts = _dicts()
    for lang in LANGS:
        assert dicts[lang].get("platform.drive_title"), lang


@pytest.mark.parametrize("lang", LANGS)
def test_every_new_key_in_every_language(lang):
    _, dicts = _dicts()
    missing = [k for k in NEW_KEYS if not dicts[lang].get(k)]
    assert missing == []


def test_translations_are_not_copies_of_english():
    _, dicts = _dicts()
    for lang in ("uk", "fr", "es"):
        same = [k for k in NEW_KEYS if dicts[lang][k] == dicts["en"][k]]
        assert same == [], lang


def test_every_drive_key_the_page_uses_exists():
    html, dicts = _dicts()
    used = set(re.findall(r'\bt\("((?:drive\.|toast\.drive)[\w.]*)"\)', html))
    assert used and all(k in dicts[lang] for lang in LANGS for k in used), sorted(used)


def test_error_categories_match_the_domain():
    html, _ = _dicts()
    shown = re.search(r"const DRIVE_ERROR_CATEGORIES = \[(.*?)\];", html).group(1)
    assert sorted(re.findall(r'"(\w+)"', shown)) == sorted(c.value for c in ErrorCategory)


# Every clause §6 requires, per language: what is copied (names, text, vectors), excerpts to the model,
# 30 days of telemetry, model-read scans, deletion on off, the 30-day keep, excerpts kept in chat.
DISCLOSURE_FRAGMENTS = {
    "en": ("names", "text it extracts", "search vectors", "AI model", "30 days", "Scans", "PDF",
           "deletes the index", "about 30 days", "conversation"),
    "uk": ("назви", "текст", "пошукові вектори", "ШІ-моделі", "30 днів", "Скани", "PDF", "видаляє індекс",
           "близько 30 днів", "розмові"),
    "fr": ("noms", "texte", "vecteurs de recherche", "modèle d'IA", "30 jours", "scans", "PDF",
           "supprime l'index", "environ 30 jours", "conversation"),
    "es": ("nombres", "texto", "vectores de búsqueda", "modelo de IA", "30 días", "escaneos", "PDF",
           "borra el índice", "unos 30 días", "conversación"),
}


@pytest.mark.parametrize("lang", LANGS)
def test_disclosure_says_what_rfc_6_requires(lang):
    _, dicts = _dicts()
    text = dicts[lang]["drive.index_disclosure"]
    missing = [f for f in DISCLOSURE_FRAGMENTS[lang] if f not in text]
    assert missing == []


def test_the_off_switch_stays_reachable_while_disconnected():
    """D3 keeps the index after a disconnect; the user can still turn search off to delete it."""
    html, _ = _dicts()
    assert "data.indexing && (linked || data.indexing.enabled)" in html
    assert 'renderDriveIndexing(data.indexing, linked)' in html
