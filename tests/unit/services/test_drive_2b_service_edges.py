"""Edge of the drive connection service in slice 2b: a settings write that finds the connection gone."""
import pytest

from src.domain.user_drive import DriveNotConnectedError
from tests.unit.services.test_drive_connection_service import World as ConnectionWorld


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

