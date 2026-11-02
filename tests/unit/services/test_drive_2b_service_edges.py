"""Edges of the drive services in slice 2b: a settings write that finds the connection gone, and the inbox of a drive
with no write folder."""
import pytest

from src.domain.drive_access_policy import NO_WRITE_FOLDER_LINE
from src.domain.drive_connection_access import ConnectionAccess
from src.domain.user_drive import DriveAccessRefused, DriveNotConnectedError
from tests.unit.services.test_drive_connection_service import AREA, World as ConnectionWorld
from tests.unit.services.test_user_drive_service_access_policy import World as DriveWorld


async def test_a_settings_write_when_the_stored_connection_vanished_is_not_connected():
    w = ConnectionWorld()
    connection = await w.connect()

    async def vanished(user_id, connection_id, **settings):
        return None
    w.repo.update_settings = vanished
    with pytest.raises(DriveNotConnectedError) as caught:
        await w.service.set_searched_by_default("usr-1", connection.connection_id, False)
    assert str(caught.value) == "The drive connection is absent"
    assert w.stored("usr-1", connection.connection_id).searched_by_default is True


async def test_the_default_inbox_of_a_drive_with_no_write_folder_is_refused_with_the_line():
    w = DriveWorld(write_folder_id=None)
    policy = (ConnectionAccess.of(w.connection, ""), AREA)
    with pytest.raises(DriveAccessRefused) as caught:
        await w.service._inbox_path(w.handle, policy)
    assert caught.value.line == NO_WRITE_FOLDER_LINE
    assert w.fake.calls == []
