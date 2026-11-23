"""Changing the passkeys over HTTP after a fresh Google sign-in (CABINET_DEVICE_SESSIONS_RFC §7.3): an iPhone whose
keychain does not hold the passkey of Chrome on a Mac adds its own after confirming with Google."""
import pytest

from tests.unit.web.test_device_auth_passkeys import (ALEX, IPHONE_HOME_SCREEN, MAC_CHROME, World,  # noqa: F401
                                                      _transactions)
from tests.support.webauthn_authenticator import SoftAuthenticator

REQUIRED = {"error": "passkey_required"}


@pytest.fixture
async def world():
    w = World()
    await w.allow(ALEX)
    return w


async def mac_passkey_then_iphone_google(world):
    """The passkey is enrolled from Chrome on the Mac; then the iPhone signs in with Google (a fresh proof)."""
    await world.google_sign_in(MAC_CHROME)
    row = (await (await world.enrol()).get_json())["passkey"]
    world.browser()
    world.authenticator = SoftAuthenticator("cabinet.example.com", "https://cabinet.example.com")   # the iPhone's keychain
    await world.google_sign_in(IPHONE_HOME_SCREEN)
    return row


async def test_the_iphone_adds_face_id_after_a_fresh_google_sign_in(world):
    await mac_passkey_then_iphone_google(world)
    assert (await world.enrol()).status_code == 201
    assert len((await (await world.client.get("/api/user/passkeys")).get_json())["passkeys"]) == 2


async def test_removing_is_200_after_a_fresh_google_sign_in(world):
    row = await mac_passkey_then_iphone_google(world)
    resp = await world.post(f"/api/user/passkeys/{row['id']}/remove")
    assert resp.status_code == 200 and await resp.get_json() == {"removed": 1}


async def test_a_stale_google_sign_in_is_400_passkey_required_at_add_and_remove(world):
    row = await mac_passkey_then_iphone_google(world)
    world.kit.clock.advance(minutes=5)
    refused = await world.post("/api/user/passkeys/register/options")
    assert refused.status_code == 400 and await refused.get_json() == REQUIRED
    resp = await world.post(f"/api/user/passkeys/{row['id']}/remove")
    assert resp.status_code == 400 and await resp.get_json() == REQUIRED
    assert len((await (await world.client.get("/api/user/passkeys")).get_json())["passkeys"]) == 1
