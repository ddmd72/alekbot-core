"""Changing the passkeys after a fresh Google sign-in (CABINET_DEVICE_SESSIONS_RFC §7.3): a device whose passkey store
does not hold the user's passkey (iPhone against Chrome on a Mac) proves the session by Google instead."""
from datetime import timedelta

import pytest

from src.domain.device_session import SessionAuthKind
from src.domain.passkey_ceremony import PasskeyFreshRequired
from tests.unit.services.test_passkey_service import ALEX, MAC_SESSION, PHONE, PHONE_SESSION, T0, world  # noqa: F401
from tests.support.webauthn_authenticator import SoftAuthenticator

STALE = timedelta(minutes=5)
OLD = T0 - timedelta(days=1)


async def with_passkey_on_a_mac(world):
    """The user's one passkey lives in Chrome on the Mac; the iPhone session signed in with Google at T0."""
    mac = MAC_SESSION.model_copy(update={"auth_at": OLD})
    await world.repo.create(mac)
    await world.repo.create(PHONE_SESSION)                                  # Google, auth_at T0
    return await world.enrol(mac)


async def test_the_iphone_adds_its_own_passkey_after_a_fresh_google_sign_in(world):
    await with_passkey_on_a_mac(world)
    start = await world.service.begin_registration(ALEX, PHONE_SESSION)
    iphone = SoftAuthenticator("cabinet.example.com", "https://cabinet.example.com")
    second = await world.service.finish_registration(start.nonce, iphone.register(start.options), ALEX, PHONE_SESSION)
    assert second.label == PHONE and len(await world.service.live_passkeys("user-alex")) == 2


async def test_removing_is_allowed_after_a_fresh_google_sign_in(world):
    stored = await with_passkey_on_a_mac(world)
    assert await world.service.remove("user-alex", stored.cred_hash, PHONE_SESSION) is True
    assert await world.service.live_passkeys("user-alex") == []


async def test_a_stale_google_sign_in_refuses_add_and_remove(world):
    stored = await with_passkey_on_a_mac(world)
    world.clock.now = T0 + STALE
    with pytest.raises(PasskeyFreshRequired):
        await world.service.begin_registration(ALEX, PHONE_SESSION)
    with pytest.raises(PasskeyFreshRequired):
        await world.service.remove("user-alex", stored.cred_hash, PHONE_SESSION)
    assert len(await world.service.live_passkeys("user-alex")) == 1


async def test_a_google_sign_in_on_another_session_does_not_help(world):
    stored = await with_passkey_on_a_mac(world)
    old_phone = PHONE_SESSION.model_copy(update={"sid": "sid-old-phone", "auth_at": OLD})
    await world.repo.create(old_phone)                                      # the Mac and the other phone are fresh
    with pytest.raises(PasskeyFreshRequired):
        await world.service.remove("user-alex", stored.cred_hash, old_phone)


async def test_a_session_object_claiming_a_fresh_google_sign_in_is_not_believed(world):
    stored = await with_passkey_on_a_mac(world)
    stale = MAC_SESSION.model_copy(update={"auth_at": OLD})                 # the stored Mac session is old
    claimed = stale.model_copy(update={"auth_at": T0, "auth_kind": SessionAuthKind.GOOGLE})
    with pytest.raises(PasskeyFreshRequired):
        await world.service.remove("user-alex", stored.cred_hash, claimed)
