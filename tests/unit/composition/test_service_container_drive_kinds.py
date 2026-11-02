"""Which drive kinds the container builds (GOOGLE_DRIVE_RFC GE-25, GE-31): each kind's adapter exists only when its
OAuth client is configured; the drive stack exists when at least one does."""
import pytest

from src.adapters.google.google_drive_adapter import GoogleDriveAdapter
from src.adapters.microsoft import OneDriveAdapter
from src.composition.connector_registry import GOOGLE_DRIVE, MICROSOFT_ONEDRIVE
from src.domain.user_drive import DriveNotConnectedError
from tests.support.drive_handles import drive_handle
from tests.unit.composition.test_drive_wiring import _container

MICROSOFT = {"MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec"}
NO_GOOGLE = {"GOOGLE_OAUTH_CLIENT_ID": "", "GOOGLE_OAUTH_CLIENT_SECRET": ""}


def _adapters(container):
    return container.connectors.drive_adapters()


def test_both_pairs_register_both_kinds_behind_the_router(monkeypatch):
    c = _container(monkeypatch, **MICROSOFT)
    adapters = _adapters(c)
    assert isinstance(adapters[GOOGLE_DRIVE], GoogleDriveAdapter)
    assert isinstance(adapters[MICROSOFT_ONEDRIVE], OneDriveAdapter)
    assert sorted(c.user_drive.kinds()) == [GOOGLE_DRIVE, MICROSOFT_ONEDRIVE]


def test_the_google_pair_alone_builds_the_drive_stack(monkeypatch):
    c = _container(monkeypatch)
    assert sorted(c.user_drive.kinds()) == [GOOGLE_DRIVE]
    assert c.user_drive_service is not None and c.drive_connection_service is not None


async def test_an_empty_google_secret_leaves_its_kind_unregistered_and_onedrive_works(monkeypatch):
    c = _container(monkeypatch, **MICROSOFT, GOOGLE_OAUTH_CLIENT_SECRET="")
    assert c.user_drive.kinds() == [MICROSOFT_ONEDRIVE]
    with pytest.raises(DriveNotConnectedError):
        await c.user_drive.get_root(drive_handle(kind=GOOGLE_DRIVE))
    assert isinstance(_adapters(c)[MICROSOFT_ONEDRIVE], OneDriveAdapter)


def test_no_pair_at_all_builds_no_drive_stack(monkeypatch):
    c = _container(monkeypatch, **NO_GOOGLE)
    assert c.user_drive is None and c.user_drive_service is None and c.drive_connection_service is None
