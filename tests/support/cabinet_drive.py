"""The Cabinet drive card's connection world (MULTI_DRIVE_RFC §4.4, §11.7): the real `DriveConnectionService` over
`FakeDriveConnectionRepository` and the real credential store (`CredentialsStore`), so the card's routes read the
connection, its switches, its state and whether its grant is stored the way production does. Technical ids only.

    world = await cabinet_drive()                     # user u1, one ACTIVE OneDrive connection, its grant stored
    world = await cabinet_drive(state=None)           # no connection at all
    create_user_cabinet_blueprint(..., drive_connections=world.connections,
                                  oauth_credentials_port=world.credentials,
                                  credential_revocation=world.revocation, connectors=ConnectorRegistry())
"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.provider_account import ProviderAccount
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.drive_connection_service import DriveConnectionService

from .drive_connection_repo import FakeDriveConnectionRepository
from .oauth_credentials_store import CredentialsStore, creds

KIND = "microsoft_onedrive"
CONNECTION_ID = "conn-1"
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


@dataclass
class CabinetDrive:
    connections: DriveConnectionService
    repo: FakeDriveConnectionRepository
    store: CredentialsStore
    user_id: str
    connection: Optional[DriveConnection]

    @property
    def credentials(self):
        """The credential port the Cabinet checks the grant with (`list_keys`)."""
        return self.store.repo

    @property
    def revocation(self) -> CredentialRevocationService:
        """The revoke rule over the same store, with no provider edge (a removal never revokes at a provider)."""
        return CredentialRevocationService(self.store.repo, {})

    async def has_grant(self) -> bool:
        keys = await self.store.repo.list_keys(self.user_id, KIND)
        return self.connection is not None and any(k.credential_id == self.connection.credential_id for k in keys)

    async def stored(self) -> Optional[DriveConnection]:
        return await self.repo.get(self.user_id, CONNECTION_ID)


async def cabinet_drive(user_id: str = "u1", *, state: Optional[ConnectionState] = ConnectionState.ACTIVE,
                        credential: bool = True, indexing: bool = False, transcribe: bool = False,
                        primary: bool = True) -> CabinetDrive:
    """A user with one OneDrive connection in `state` (None: no connection), its grant stored unless `credential` is
    False, the switches given, and the primary pointer on it unless `primary` is False."""
    store = CredentialsStore()
    repo = FakeDriveConnectionRepository()
    service = DriveConnectionService(repo, store.repo, clock=lambda: T0)
    connection: Optional[DriveConnection] = None
    if state is not None:
        grant = creds(user_id, KIND, account=ProviderAccount(realm="microsoft", subject="sub-1", label="x"))
        credential_id = await store.repo.save(grant) if credential else "cred-gone"
        connection = DriveConnection(connection_id=CONNECTION_ID, user_id=user_id, account_id="a1",
                                     credential_id=credential_id, kind=KIND,
                                     drive_identity="microsoft_onedrive:d1", ref_tag="a1",
                                     indexing_enabled=indexing, transcribe_enabled=transcribe, created_at=T0)
        await repo.create(connection)
        if state is not ConnectionState.ACTIVE:
            await repo.transition(user_id, CONNECTION_ID, state, T0)
            connection = replace(connection, state=state,
                                 disconnected_at=T0 if state is ConnectionState.DISCONNECTED else None)
        if primary and state is ConnectionState.ACTIVE:
            await service.set_primary(user_id, CONNECTION_ID)
    return CabinetDrive(connections=service, repo=repo, store=store, user_id=user_id, connection=connection)
