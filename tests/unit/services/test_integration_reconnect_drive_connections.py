"""R1 for the drive reads drive connections (MULTI_DRIVE_RFC §4.4; decisions/reconnect_notice_r1.md): the Cabinet shows
Reconnect for a NEEDS_RECONNECT connection or an ACTIVE one whose grant is gone, never for a DISCONNECTED one; the
drive has no disconnect mark (its disconnect ends the connection) and a consent clears nothing here. The real
`DriveConnectionService` over the in-memory store and the real credential store on `EvalDb`."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.composition.connector_registry import ConnectorRegistry
from src.domain.integration import Integration
from src.domain.provider_account import ProviderAccount
from src.services.drive_connection_service import DriveConnectionService
from src.services.integration_reconnect_service import IntegrationReconnectService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.oauth_credentials_store import CredentialsStore, creds

USER, KIND = "usr-1", "microsoft_onedrive"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
DRIVE = (Integration.DRIVE,)


class World:
    def __init__(self, wired: bool = True) -> None:
        self.store = CredentialsStore()
        self.repo = FakeDriveConnectionRepository()
        self.connections = DriveConnectionService(self.repo, self.store.repo, clock=lambda: T0)
        registry = ConnectorRegistry()
        self.svc = IntegrationReconnectService(
            credentials=self.store.repo, users=MagicMock(), notifier=None,
            drive_connections=self.connections if wired else None, clock=lambda: T0,
            mail_connector=registry.get("gmail"), todo_connector=registry.get("microsoft_todo"))

    async def connect(self, subject="subject-a", identity="microsoft_onedrive:drive-a"):
        credential = creds(USER, KIND, account=ProviderAccount(realm="microsoft", subject=subject, label="x"))
        saved = replace(credential, credential_id=await self.store.repo.save(credential))
        return await self.connections.activate(saved, identity)


async def test_no_connection_needs_no_reconnect():
    assert await World().svc.status(USER, DRIVE) == {"drive": False}


async def test_a_healthy_connection_needs_none():
    w = World()
    await w.connect()
    assert await w.svc.status(USER, DRIVE) == {"drive": False}


async def test_a_flagged_connection_needs_a_reconnect():
    w = World()
    conn = await w.connect()
    await w.connections.flag_reconnect(conn.handle)
    assert await w.svc.status(USER, DRIVE) == {"drive": True}


async def test_an_active_connection_without_its_grant_needs_a_reconnect():
    w = World()
    conn = await w.connect()
    await w.store.repo.delete(USER, conn.credential_id)
    assert await w.svc.status(USER, DRIVE) == {"drive": True}


async def test_a_disconnected_connection_never_needs_one():
    w = World()
    conn = await w.connect()
    await w.connections.disconnect(USER, conn.connection_id)
    await w.store.repo.delete(USER, conn.credential_id)
    assert await w.svc.status(USER, DRIVE) == {"drive": False}


async def test_a_consent_back_clears_it():
    w = World()
    conn = await w.connect()
    await w.connections.flag_reconnect(conn.handle)
    await w.connect()
    assert await w.svc.status(USER, DRIVE) == {"drive": False}


async def test_unwired_reads_no_reconnect():
    assert await World(wired=False).svc.status(USER, DRIVE) == {"drive": False}


async def test_the_drive_has_no_disconnect_mark():
    w = World()
    with pytest.raises(ValueError):
        await w.svc.mark_disconnected(USER, Integration.DRIVE)


async def test_a_drive_consent_clears_nothing_here():
    w = World()
    conn = await w.connect()
    await w.connections.flag_reconnect(conn.handle)
    await w.svc.consented(USER, Integration.DRIVE)
    assert await w.svc.status(USER, DRIVE) == {"drive": True}       # only the activation returns it to ACTIVE
