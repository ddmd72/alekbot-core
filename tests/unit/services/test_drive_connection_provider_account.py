"""The provider account a connection was activated with (MULTI_DRIVE_RFC §10, D8): activation stores it (label empty),
a reconnect refreshes it, the scheduler's initial-build read and the indexer's tick read it from the connection and
never decrypt the credential; a connection without one is a programming error: ERROR log, no initial build counted. Technical ids only."""
import logging
from dataclasses import replace

from src.domain.provider_account import ProviderAccount
from tests.support.drive_connection_service_fake import connection

from .drive_multi_connection_world import MultiWorld
from .test_drive_connection_service import World as ConnectionWorld
from .test_drive_sync_fan_out_connections import USER, World as FanOutWorld

ACCOUNT = ("microsoft", "subject-a")
ACCOUNT_X = ProviderAccount(realm="realm-x", subject="sub-1", label="")


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


async def test_the_initial_build_is_read_from_the_connections_account():
    w = FanOutWorld()
    conn = await w.connections.add(replace(connection(USER, "conn-1"), provider_account=ACCOUNT_X))
    await w.repo.spend_initial_build(USER, "realm-x", "sub-1", 40.0, w.clock.now)
    assert (await w.svc._initial_build(conn)).spent_usd == 40.0


async def test_a_connection_without_an_account_logs_an_error_and_counts_no_initial_build(caplog):
    w = FanOutWorld()
    conn = await w.connections.add(connection(USER, "conn-1"))
    await w.repo.spend_initial_build(USER, "realm-x", "sub-1", 40.0, w.clock.now)
    with caplog.at_level(logging.ERROR):
        build = await w.svc._initial_build(conn)
    assert build.closed_at is not None and build.spent_usd == 0.0
    assert any(r.levelno == logging.ERROR and "no provider account" in r.getMessage() for r in caplog.records)


async def test_the_indexer_logs_an_error_and_counts_no_account_for_a_connection_without_one(caplog):
    w = MultiWorld()
    conn = await w.connections.add(connection(USER, "conn-1"))
    with caplog.at_level(logging.ERROR):
        account = await w.svc._account(conn.handle, conn)
    assert account is None
    assert any(r.levelno == logging.ERROR and "no provider account" in r.getMessage() for r in caplog.records)
    with_account = replace(conn, provider_account=ACCOUNT_X)
    assert await w.svc._account(with_account.handle, with_account) == ACCOUNT_X
