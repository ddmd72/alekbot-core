"""The Cabinet page's drive section (MULTI_DRIVE_RFC §11.7, §14 row 2b): no drive kind and no provider name is written
in the static page (the kind picker reads GET /api/connectors), and every /api/drive/ URL the page fetches is a route
the blueprint serves."""
import re
from pathlib import Path
from unittest.mock import MagicMock

from quart import Quart

from src.composition.connector_registry import ConnectorRegistry
from src.domain.integration import Integration
from src.web.user_cabinet_app import create_user_cabinet_blueprint

_HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html").read_text(encoding="utf-8")


def _routes() -> list:
    app = Quart("static_2b")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=MagicMock(), user_repo=MagicMock(), fact_repo=MagicMock(),
        embedding_service=MagicMock(), connectors=ConnectorRegistry()))
    return [(rule.rule, rule.methods) for rule in app.url_map.iter_rules()]


def test_no_drive_kind_and_no_provider_word_in_the_page():
    registry = ConnectorRegistry()
    kinds = registry.kinds(Integration.DRIVE)
    assert kinds
    for kind in kinds:
        spec = registry.get(kind)
        assert kind not in _HTML, kind
        assert spec.label not in _HTML, spec.label
    for word in ("onedrive", "google drive", "google_drive", "gdrive"):
        assert word not in _HTML.lower(), word


def test_the_add_drive_kinds_come_from_the_connector_list():
    assert 'c.integration === "drive"' in _HTML
    assert "?intent=add" in _HTML and "sessionStorage" in _HTML


def test_every_drive_url_the_page_fetches_is_a_route():
    served = [re.sub(r"<[^>]+>", "{}", rule) for rule, _ in _routes()]
    urls = set(re.findall(r"/api/drive/(?:[A-Za-z0-9_\-/]|\$\{[^}]*\})*", _HTML))
    assert urls
    for url in urls:
        shape = re.sub(r"\$\{[^}]*\}", "{}", url).rstrip("/")
        assert shape in served, shape


def test_no_fetch_of_a_retired_route_remains():
    for gone in ("/api/drive/status", "/api/drive/indexing", "/api/drive/disconnect"):
        assert gone not in _HTML
