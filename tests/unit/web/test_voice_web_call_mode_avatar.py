"""The avatar inside a mode page shows the mode's own icon; the plain call page keeps Lelik's."""
import pytest

from tests.unit.web.test_voice_web_call_modes import _app

MODES = ["interpreter", "tutor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_mode_page_avatar_shows_the_mode_icon(mode):
    client = _app()[0].test_client()
    resp = await client.get(f"/cabinet/call/{mode}")
    html = (await resp.get_data()).decode()
    assert resp.status_code == 200
    assert f'id="avatar" src="/cabinet/call/{mode}/icon-192.png"' in html
    assert 'id="avatar" src="/cabinet/call/icon-192.png"' not in html
    # The icon it points at is served.
    assert (await client.get(f"/cabinet/call/{mode}/icon-192.png")).status_code == 200


@pytest.mark.asyncio
async def test_plain_call_page_avatar_stays_lelik():
    client = _app()[0].test_client()
    resp = await client.get("/cabinet/call")
    html = (await resp.get_data()).decode()
    assert 'id="avatar" src="/cabinet/call/icon-192.png"' in html
