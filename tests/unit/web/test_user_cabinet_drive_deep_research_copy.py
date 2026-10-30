"""Cabinet: "Copy deep research reports to my drive" (decisions/deep_research_drive_copy.md).

The toggle is the user's explicit consent; default off. GET/PUT /api/drive/deep-research-copy, the same
shape as /api/drive/indexing.
"""
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.user import UserBotConfig
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry

_AUTH = {"Authorization": "Bearer token"}
_URL = "/api/drive/deep-research-copy"
_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"
LANGS = ("en", "uk", "fr", "es")
KEYS = ("drive.dr_copy_title", "drive.dr_copy_disclosure", "drive.index_disclosure_dr_copy",
        "toast.drive_dr_copy_on", "toast.drive_dr_copy_off", "toast.drive_dr_copy_failed")


def _users(enabled=False, missing=False):
    repo = MagicMock()
    profile = SimpleNamespace(config=SimpleNamespace(drive_copy_deep_research=enabled))
    repo.get_user = AsyncMock(return_value=None if missing else profile)
    repo.update_user = AsyncMock()
    return repo, profile


def _app(user_repo):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_drive_dr_copy")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=user_repo,
        fact_repo=MagicMock(), embedding_service=MagicMock(), connectors=ConnectorRegistry(),
    ))
    return app


def test_default_is_off():
    assert UserBotConfig().drive_copy_deep_research is False


class TestGet:
    @pytest.mark.parametrize("enabled", [False, True])
    async def test_reads_the_flag(self, enabled):
        repo, _ = _users(enabled=enabled)
        async with _app(repo).test_client() as client:
            resp = await client.get(_URL, headers=_AUTH)
        assert resp.status_code == 200 and (await resp.get_json()) == {"enabled": enabled}

    async def test_unknown_user(self):
        repo, _ = _users(missing=True)
        async with _app(repo).test_client() as client:
            assert (await client.get(_URL, headers=_AUTH)).status_code == 404

    async def test_requires_auth(self):
        repo, _ = _users()
        async with _app(repo).test_client() as client:
            assert (await client.get(_URL)).status_code == 401


class TestPut:
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_saves_the_flag(self, enabled):
        repo, profile = _users(enabled=not enabled)
        async with _app(repo).test_client() as client:
            resp = await client.put(_URL, headers=_AUTH, json={"enabled": enabled})
        assert resp.status_code == 200 and (await resp.get_json()) == {"enabled": enabled}
        assert profile.config.drive_copy_deep_research is enabled
        repo.update_user.assert_awaited_once_with(profile)

    @pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}])
    async def test_bool_required(self, body):
        repo, _ = _users()
        async with _app(repo).test_client() as client:
            assert (await client.put(_URL, headers=_AUTH, json=body)).status_code == 400
        repo.update_user.assert_not_called()

    async def test_unknown_user(self):
        repo, _ = _users(missing=True)
        async with _app(repo).test_client() as client:
            assert (await client.put(_URL, headers=_AUTH, json={"enabled": True})).status_code == 404

    async def test_store_error(self):
        repo, _ = _users()
        repo.update_user.side_effect = RuntimeError("firestore down")
        async with _app(repo).test_client() as client:
            assert (await client.put(_URL, headers=_AUTH, json={"enabled": True})).status_code == 500


def _dicts():
    html = _HTML.read_text(encoding="utf-8")
    body = re.search(r"const LANG_DICT = \{(.*?)\n      \};", html, re.S).group(1)
    out = {}
    for lang in LANGS:
        block = re.search(rf"\n        {lang}: \{{(.*?)\n        \}},", body, re.S).group(1)
        out[lang] = dict(re.findall(r'^\s*"([\w.]+)": "((?:[^"\\]|\\.)*)",?$', block, re.M))
    return html, out


class TestCabinetPage:
    @pytest.mark.parametrize("lang", LANGS)
    def test_every_string_in_every_language(self, lang):
        _, dicts = _dicts()
        assert [k for k in KEYS if not dicts[lang].get(k)] == []

    def test_translations_are_not_copies_of_english(self):
        _, dicts = _dicts()
        for lang in ("uk", "fr", "es"):
            assert [k for k in KEYS if dicts[lang][k] == dicts["en"][k]] == [], lang

    def test_the_page_calls_the_endpoint_and_uses_the_strings(self):
        html, _ = _dicts()
        assert "/api/drive/deep-research-copy" in html
        for key in ("drive.dr_copy_title", "drive.dr_copy_disclosure", "drive.index_disclosure_dr_copy"):
            assert f't("{key}")' in html, key

    @pytest.mark.parametrize("lang, folder", [("en", "Deep research"), ("uk", "Глибокі дослідження"),
                                              ("fr", "Recherches approfondies"), ("es", "Investigaciones")])
    def test_disclosure_names_the_folder(self, lang, folder):
        _, dicts = _dicts()
        assert folder in dicts[lang]["drive.dr_copy_disclosure"]
