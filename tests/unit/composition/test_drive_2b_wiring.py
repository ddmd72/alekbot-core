"""Slice 2b wiring (MULTI_DRIVE_RFC §11.3, §11.6, §11.7): the access policy's service reaches every consumer that
reads a drive item, the deleted-files notes source sees the live connections, and the disconnect flow is built for the
Cabinet. The container is the real one over the deploy's drive keys (as test_drive_wiring)."""
import pytest

from src.services.drive_disconnect_service import DriveDisconnectService
from src.services.user_drive_service import UserDriveService
from tests.support.prod_graph import prod_graph  # noqa: F401  (the fixture)
from tests.unit.composition.test_drive_wiring import _container


def _wired(monkeypatch):
    return _container(monkeypatch, MICROSOFT_TODO_CLIENT_ID="cid", MICROSOFT_TODO_CLIENT_SECRET="sec",
                      GCS_MEDIA_BUCKET="media-bucket")


def test_every_consumer_of_a_drive_item_holds_the_access_service(monkeypatch):
    c = _wired(monkeypatch)
    assert isinstance(c.user_drive_service, UserDriveService)
    assert c.file_conversion_service._access is c.user_drive_service
    assert c.drive_note_service._access is c.user_drive_service


async def test_the_deleted_notes_source_reads_the_live_connection_tags(monkeypatch):
    c = _wired(monkeypatch)
    source = c.drive_search_service._deleted_sources[-1]
    assert source._live_tags is not None

    class Live:
        ref_tag = "a1"

    async def live(user_id):
        return [Live()]
    c.drive_connection_service.list_for_user = live
    assert await source._live_tags("usr-1") == frozenset({"a1"})


@pytest.mark.slow
async def test_the_production_graph_builds_the_disconnect_flow_on_the_container_services(prod_graph):
    app = prod_graph.app
    flow = app.drive_disconnect_service
    assert isinstance(flow, DriveDisconnectService)
    assert flow._connections is app.container.drive_connection_service
    assert flow._revocation is app.credential_revocation
    assert flow._drive is app.container.user_drive and flow._notes is app.container.drive_note_repo
    assert app.container.drive_note_service._access is app.container.user_drive_service
    assert app.container.file_conversion_service._access is app.container.user_drive_service
