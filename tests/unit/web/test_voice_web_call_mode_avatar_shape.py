"""A mode page shows its square app icon with rounded corners; the plain call page keeps the round avatar."""
import pytest

from tests.unit.web.test_voice_web_call_modes import _app

MODES = ["interpreter", "tutor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_mode_page_avatar_is_a_rounded_app_icon(mode):
    html = (await (await _app()[0].test_client().get(f"/cabinet/call/{mode}")).get_data()).decode()
    assert f'class="avatar app-icon" id="avatar" src="/cabinet/call/{mode}/icon-192.png"' in html
    assert ".avatar.app-icon {" in html


@pytest.mark.asyncio
async def test_plain_call_page_avatar_stays_round():
    html = (await (await _app()[0].test_client().get("/cabinet/call")).get_data()).decode()
    assert 'class="avatar" id="avatar" src="/cabinet/call/icon-192.png"' in html
    assert "app-icon\" id=\"avatar\"" not in html
