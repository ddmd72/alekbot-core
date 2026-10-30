"""A consent whose activation refuses it is a failed connect (MULTI_DRIVE_RFC §4.4, §5.2; owner decision for slice 1b):
a drive grant whose drive identity cannot be read gets no connection, so its just-saved credential is removed (through
the injected removal, the revoke rule) and the outcome is `activation`, which the Cabinet shows as the ordinary connect
error. Any other activation failure keeps the connect (the existing rule). The real `OAuthConnectService` over the real
credential store, the real drive activation over the in-memory connection store; the edge and the drive are stand-ins."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from src.composition.drive_connection_setup import build_drive_activation
from src.domain.connector_spec import ConnectorSpec
from src.domain.drive_handle import DriveHandle
from src.domain.integration import Integration
from src.domain.oauth_grant import GrantResult
from src.domain.provider_account import ProviderAccount
from src.domain.revocation_scope import RevocationScope
from src.domain.user_drive import DriveItem
from src.ports.ephemeral_store import EphemeralStore
from src.ports.oauth_provider_port import OAuthProviderPort
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.drive_connection_service import DriveConnectionService
from src.services.oauth_connect_service import OAuthConnectService
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.oauth_credentials_store import CredentialsStore, creds

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
USER = "usr-1"
DRIVE = ConnectorSpec(kind="fake_a", integration=Integration.DRIVE, realm="fake", required_scopes=("files",),
                      multiple=True, id_space="fake_a", label="Fake drive")


class Flows(EphemeralStore):
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ttl_s):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


class Edge(OAuthProviderPort):
    realm = "fake"
    revocation_scope = RevocationScope.GRANT_WIDE

    def __init__(self):
        self.revoked = []

    def authorization_url(self, flow, spec, redirect_uri, *, state):
        return f"https://idp.example/auth?state={state}"

    async def exchange(self, code, flow, spec, redirect_uri):
        return GrantResult(access_token="at", refresh_token="rt", token_expiry=T0 + timedelta(hours=1),
                           scopes=("files",), account=ProviderAccount(realm="fake", subject="subject-a", label="x"))

    async def revoke(self, credential):
        self.revoked.append(credential.credential_id)


class Drive:
    def __init__(self, identity):
        self.identity = identity

    async def get_root(self, handle: DriveHandle) -> DriveItem:
        if not self.identity:
            raise ConnectionError("provider unreachable")
        return DriveItem(item_id="root", name="", path="", is_folder=True, drive_identity=self.identity)

    async def forget(self, handle: DriveHandle) -> None:
        return None


class World:
    def __init__(self, identity, activation=None):
        self.store = CredentialsStore()
        self.repo = FakeDriveConnectionRepository()
        self.edge = Edge()
        connections = DriveConnectionService(self.repo, self.store.repo, clock=lambda: T0)
        drive_activation = activation or build_drive_activation(connections=connections, drive=Drive(identity),  # type: ignore[arg-type]
                                                                 task_queue=None)
        revocation = CredentialRevocationService(self.store.repo, {"fake": self.edge})
        self.service = OAuthConnectService(
            self.store.repo, {DRIVE.kind: DRIVE}, {"fake": self.edge}, Flows(), AesGcmTestCipher(), b"k" * 32,
            revocation.remove, {Integration.DRIVE: drive_activation}, "https://example.test/cb", clock=lambda: T0)

    async def connect(self):
        url = await self.service.start(USER, DRIVE.kind)
        state = parse_qs(urlparse(url).query)["state"][0]
        return await self.service.complete(state, "code-1", session_user_id=USER, session_account_id="acc-1")


async def test_an_unreadable_drive_identity_fails_the_connect_and_leaves_no_credential(caplog):
    w = World(identity="")
    outcome = await w.connect()
    assert (outcome.status, outcome.kind) == ("activation", DRIVE.kind)
    assert await w.store.repo.list_keys(USER, DRIVE.kind) == []
    assert w.repo.connections == {}
    assert len(w.edge.revoked) == 1         # the revoke rule: a grant-wide realm and no credential of the account left
    assert "refused the consent" in caplog.text


async def test_no_provider_revoke_while_another_credential_of_the_account_remains():
    w = World(identity="")
    await w.store.repo.save(creds("usr-2", "microsoft_onedrive",
                                  account=ProviderAccount(realm="fake", subject="subject-a", label="x")))
    assert (await w.connect()).status == "activation"
    assert await w.store.repo.list_keys(USER, DRIVE.kind) == []
    assert w.edge.revoked == []


async def test_a_readable_identity_connects_with_its_connection():
    w = World(identity="fake_a:drive-a")
    outcome = await w.connect()
    assert outcome.status == "connected"
    (key,) = await w.store.repo.list_keys(USER, DRIVE.kind)
    (conn,) = w.repo.connections.values()
    assert conn.credential_id == key.credential_id and conn.drive_identity == "fake_a:drive-a"


async def test_another_activation_failure_keeps_the_connect():
    async def broken(credential):
        raise RuntimeError("boom")
    w = World(identity="", activation=broken)
    assert (await w.connect()).status == "connected"
    assert len(await w.store.repo.list_keys(USER, DRIVE.kind)) == 1


async def test_a_failed_removal_still_fails_the_connect(caplog):
    w = World(identity="")

    async def broken(user_id, credential_id):
        raise RuntimeError("store down")
    w.store.repo.delete = broken
    outcome = await w.connect()
    assert outcome.status == "activation"
    assert "Removing the refused credential failed" in caplog.text
