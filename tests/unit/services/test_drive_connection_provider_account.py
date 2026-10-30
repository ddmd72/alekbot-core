"""The provider account a connection was activated with (MULTI_DRIVE_RFC §10, D8): activation stores it (label empty),
a reconnect refreshes it, the scheduler's initial-build read and the indexer's tick read it from the connection and
never decrypt the credential; a connection stored before the field decrypts once and is backfilled. Technical ids only."""
from tests.support.drive_connection_service_fake import connection

from .test_drive_connection_service import World as ConnectionWorld
from .test_drive_sync_fan_out_connections import USER, World as FanOutWorld

ACCOUNT = ("microsoft", "subject-a")


async def test_activation_stores_the_credentials_account_without_its_label():
    w = ConnectionWorld()
    created = await w.connect(subject="subject-a")
    stored = w.stored("usr-1", created.connection_id)
    assert (stored.provider_account.realm, stored.provider_account.subject, stored.provider_account.label) == (
        *ACCOUNT, "")


async def test_a_reconnect_by_drive_identity_with_another_account_replaces_the_stored_account():
    w = ConnectionWorld()
    first = await w.connect(subject="subject-a")
    again = await w.connect(subject="subject-b")           # same drive identity: reconnect to a new grant
    assert again.connection_id == first.connection_id
    assert w.stored("usr-1", first.connection_id).provider_account.subject == "subject-b"


async def test_the_initial_build_is_read_from_the_connection_without_touching_the_credential():
    w = FanOutWorld()
    calls = []

    async def account_of(user_id, credential_id):
        calls.append(credential_id)
        return None

    w.svc._account_of = account_of
    conn = await w.connections.add(connection(USER, "conn-1"))
    await w.connections.repo.update(USER, "conn-1", provider_account=w.accounts["cred-1"])
    await w.repo.spend_initial_build(USER, "realm-x", "sub-1", 40.0, w.clock.now)
    stored = await w.connections.repo.get(USER, conn.connection_id)
    assert (await w.svc._initial_build(stored)).spent_usd == 40.0
    assert calls == []


async def test_a_connection_without_the_field_decrypts_once_and_is_backfilled():
    w = FanOutWorld()
    conn = await w.connections.add(connection(USER, "conn-1"))
    await w.repo.spend_initial_build(USER, "realm-x", "sub-1", 40.0, w.clock.now)
    assert (await w.svc._initial_build(conn)).spent_usd == 40.0
    stored = await w.connections.repo.get(USER, "conn-1")
    assert (stored.provider_account.realm, stored.provider_account.subject) == ("realm-x", "sub-1")
    w.accounts.clear()                                      # the credential is gone: the stored account still counts
    assert (await w.svc._initial_build(stored)).spent_usd == 40.0
