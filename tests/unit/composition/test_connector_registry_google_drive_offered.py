"""`google_drive` is an offered kind (GOOGLE_DRIVE_RFC GE-24): after OneDrive in `offered()`, listed by
`GET /api/connectors` as "Google Drive" when the Google edge is configured."""
from src.composition.connector_registry import ConnectorRegistry
from tests.unit.web.test_cabinet_connectors_and_revoke import AUTH, World


def test_it_is_offered_after_onedrive():
    kinds = [spec.kind for spec in ConnectorRegistry().offered()]
    assert kinds == ["gmail", "microsoft_todo", "microsoft_onedrive", "google_drive"]
    assert ConnectorRegistry().get("google_drive").offered is True


async def test_the_cabinet_lists_it_with_its_label(monkeypatch):
    world = World(monkeypatch)
    async with world.app().test_client() as client:
        body = await (await client.get("/api/connectors", headers=AUTH)).get_json()
    assert {"kind": "google_drive", "integration": "drive", "label": "Google Drive"} in body["connectors"]


async def test_without_the_google_edge_it_is_not_listed(monkeypatch):
    world = World(monkeypatch)
    world.registry = ConnectorRegistry(edges={})
    async with world.app().test_client() as client:
        body = await (await client.get("/api/connectors", headers=AUTH)).get_json()
    assert "google_drive" not in [c["kind"] for c in body["connectors"]]
