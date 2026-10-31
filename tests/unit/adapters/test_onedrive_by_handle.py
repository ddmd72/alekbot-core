"""OneDrive adapter by handle: the grant is the handle's own, state is per connection, secrets stay out of logs."""
import logging

import pytest

from src.domain.user_drive import DriveNotConnectedError
from tests.support.drive_handles import drive_handle
from tests.unit.adapters import test_onedrive_drive_contract as _contract


world = _contract.world        # the two-account world fixture


async def test_a_handle_whose_credential_is_gone_is_not_connected(world):
    ghost = drive_handle("usr-1", "conn-9", "cred-missing")
    with pytest.raises(DriveNotConnectedError):
        await world.port.get_root(ghost)


async def test_a_thumbnail_asks_for_the_edge_as_a_scaled_custom_size(world):
    h = world.handle
    pic = await world.plant_thumbnail(h, (await world.port.get_root(h)).item_id, "p.jpg", b"\xff\xd8x")
    assert await world.port.get_thumbnail(h, pic, 640) == b"\xff\xd8x"
    asked = [r.url for r in world.fake.requests if "/thumbnails" in r.url]
    assert asked and asked[-1].endswith("/thumbnails?select=c640x640")


@pytest.mark.parametrize("edge", [0, -5, 10000, True, "800", 800.0])
async def test_a_bad_thumbnail_edge_never_reaches_the_wire(world, edge):
    before = len(world.fake.requests)
    with pytest.raises(ValueError):
        await world.port.get_thumbnail(world.handle, "item-1", edge)
    assert len(world.fake.requests) == before


async def test_the_download_url_is_never_logged_nor_sent_a_bearer(world, caplog):
    caplog.set_level(logging.DEBUG)
    h = world.handle
    item = await world.port.upload(h, (await world.port.get_root(h)).item_id, "s.bin", b"s", "application/octet-stream")
    source = await world.port.media_source(h, item.item_id)
    assert source.header_free and source.expires_at is None
    assert source.url not in caplog.text and "dl.example" not in caplog.text and "example.invalid/drive" not in caplog.text
    assert all(r.url != source.url for r in world.fake.requests)        # the URL is returned, not fetched


async def test_forget_drops_the_token_and_the_root_of_that_connection_only(world):
    a, b = world.handle, world.other
    await world.port.get_root(a)
    await world.port.get_root(b)
    await world.port.forget(a)
    assert world.adapter._tokens.cached_token(a.user_id, a.credential_id) is None
    assert a.connection_id not in world.adapter._root_cache
    assert world.adapter._tokens.cached_token(b.user_id, b.credential_id) == "tok-b"
    assert b.connection_id in world.adapter._root_cache


async def test_capabilities_are_the_app_folder_limit(world):
    assert (await world.port.capabilities(world.handle)).max_path_chars == 400


async def test_get_changes_names_the_drive_by_identity_and_bound_cursor(world):
    page = await world.port.get_changes(world.handle, None)
    assert page.drive_identity == "microsoft_onedrive:drive-a"
    other = await world.port.get_changes(world.other, None)
    assert other.drive_identity == "microsoft_onedrive:drive-b"
    # a cursor from the other connection's drive is never followed: the enumeration restarts
    from src.domain.user_drive import DriveResyncRequired
    with pytest.raises(DriveResyncRequired):
        await world.port.get_changes(world.other, page.cursor)
