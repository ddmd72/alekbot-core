"""I8, "a reconnect does not re-index" (SECRETS_AT_REST_RFC §6.2, §8 I8; I4 on the indexer path).

After the credential cutover the new store starts empty: every integration reads "not connected" until the user
consents once. This file drives that consent through the real chain and asserts what the feature stores hold after it.

Real: the oauth blueprint's generic connect routes (`/auth/connect/<kind>`, `/auth/connect/callback`, MULTI_DRIVE_RFC
§5.2) over the real `SessionService`, `OAuthConnectService` with its flows in `FirestoreEphemeralStore`, the Google and
Microsoft edges and the activations (`build_connect_activations`: the drive activation, `build_drive_activation`, over
the real `DriveConnectionService` with the drive identity read by `OneDriveAdapter.get_root` behind the router; the To
Do setup task); R1's `consented` is a spec'd recorder. `FirestoreCredentialStore` + `AesGcmTestCipher`,
`FirestoreIndexedEmailRepository`, `FirestoreTaskConfigRepository`, `FirestoreTaskSearchIndex`, `TaskSetupService` (its task
delivered through the real `WorkerHandler` dispatch),
`DriveIndexingService` over `InMemoryDriveIndexRepository`, ticking the connection read from the same
`DriveConnectionService` (MULTI_DRIVE_RFC §4.4, §8), `GcpTaskQueue` over
`tests/support/cloud_tasks_fake.py` (the main service's worker queue); all Firestore on one `EvalDb`.
Fakes: HTTP through `tests/support/http_boundary_fake.py` (token endpoints, Google userinfo, Graph `/me`, approot); the
drive's I/O through `FakeContentDrive` with the adapter's credential read in front of its feed; the drive connections
on `FakeDriveConnectionRepository`; To Do's Graph subscription
lifecycle (`GraphSubscriptions`, subscriptions with an expiry). The drive tick's own continuation queue is the drive
harness's `FakeQueue` (the indexer's queue, not the worker queue).

Outcomes by integration:
- Gmail holds: a consent writes only the credential and enqueues no indexing.
- Drive (MULTI_DRIVE_RFC §4.4): a lost grant flags the connection and the index holds within the grace; a consent of
  the same drive reconnects the same connection, which resumes from the stored cursor, nothing purged. Another drive
  purges by design (drive switch). A reconnect over 30 days after the loss purges (expired), and the next ticks rebuild
  the index from scratch: a paid rebuild (card embeddings, content and enrichment work again). A credential that is
  simply gone is no loss on the index: the index is kept and R1 owes the connection its reconnect notice.
- To Do: a consent re-subscribes whatever the stored subscription says (active or lapsed): the stored one is dropped,
  one is registered and the primary list is re-read exactly once (`reindex_task_list`), an upsert that deletes nothing.
  A stored "active" subscription may have been dropped by Graph with the old consent, so trusting it would lose the
  changes made during the gap. The renewal sweep still keeps an active subscription.
- I4: a cipher that fails on decrypt fails the tick; no loss is recorded, nothing is purged.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import Mock, create_autospec
from urllib.parse import parse_qs, urlparse

import pytest
from quart import Quart

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.adapters.firestore_task_search_index import FirestoreTaskSearchIndex
from src.adapters.gcp_task_queue import GcpTaskQueue
from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.adapters.microsoft import OneDriveAdapter
from src.adapters.microsoft.microsoft_oauth_provider import MicrosoftOAuthProvider
from src.composition.drive_connection_setup import build_drive_activation
from src.composition.drive_provider_router import DriveProviderRouter
from src.composition.oauth_connect_setup import build_connect_activations, build_oauth_connect_service
from src.domain.billing import BillingAccount
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_extraction import PARSER, READER
from src.domain.drive_index_params import ACCESS_LOSS_TTL
from src.domain.email import IndexingState
from src.domain.task import TaskSubscriptionConfig, TaskUserConfig
from src.domain.user import UserProfile
from src.domain.user_drive import DriveNotConnectedError
from src.ports.task_lifecycle_port import TaskLifecyclePort
from src.handlers.worker_handler import WorkerHandler
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.drive_connection_service import DriveConnectionService
from src.services.drive_indexing_service import DriveIndexingService
from src.services.integration_reconnect_service import IntegrationReconnectService
from src.services.session_service import SessionService
from src.services.task_setup_service import TaskSetupService
from src.web.oauth_app import create_oauth_blueprint
from tests.support import http_boundary_fake
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.cloud_tasks_fake import FakeCloudTasksClient
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.firestore_eval import EvalDb
from tests.unit.services.drive_content_fakes import FakeExtractor, FakeTriage
from tests.unit.services.test_drive_indexing_service import World as DriveWorld
from tests.support.oauth_credentials_store import has_kind
from src.composition.connector_registry import ConnectorRegistry

USER = "u"   # the drive harness's user
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"
MS_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
APPROOT = "https://graph.microsoft.com/v1.0/me/drive/special/approot"
ENV = SimpleNamespace(
    credentials_collection="eval_credentials", domain_email_facts_collection="eval_email_facts",
    email_indexing_state_collection="eval_email_state", task_config_collection="eval_task_config",
    task_search_index_collection="eval_task_search")
SECRET = "session-secret-for-tests-0123456789abcdef"
REDIRECT = "https://service.example/auth/connect/callback"
GRANTED = {   # each realm's token endpoint lists the granted scopes
    "gmail": "https://www.googleapis.com/auth/gmail.readonly openid https://www.googleapis.com/auth/userinfo.email",
    "microsoft_todo": "Tasks.ReadWrite offline_access User.Read",
    "microsoft_onedrive": "Files.ReadWrite.AppFolder offline_access User.Read",
}
INDEXED_THROUGH = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
OLDEST = datetime(2026, 1, 3, 8, 0, tzinfo=timezone.utc)


class FailingDecrypt(AesGcmTestCipher):
    """Encrypts; decrypt fails while `failing` is set (a KMS outage or a lost permission)."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = False

    async def decrypt(self, ciphertext: str, *, aad: str) -> str:
        if self.failing:
            raise RuntimeError("KMS decrypt unavailable")
        return await super().decrypt(ciphertext, aad=aad)


class GraphSubscriptions(TaskLifecyclePort):
    """To Do's Graph lifecycle with semantics: the primary list exists, a subscription lives three days."""

    def __init__(self) -> None:
        self.live: dict = {}
        self.registered: List[str] = []

    async def ensure_primary_list(self, user_id: str) -> str:
        return "l1"

    async def register_subscription(self, user_id: str, list_id: str, notification_url_base: str):
        sub_id = f"s{len(self.registered) + 1}"
        self.registered.append(sub_id)
        sub = TaskSubscriptionConfig(sub_id=sub_id, list_id=list_id,
                                     expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3))
        self.live[sub_id] = sub
        return sub

    async def renew_subscription(self, user_id: str, sub_id: str):
        raise AssertionError("no renewal in a consent")

    async def delete_subscription(self, user_id: str, sub_id: str) -> None:
        self.live.pop(sub_id, None)


class World:
    def __init__(self, monkeypatch) -> None:
        monkeypatch.setattr(FakeCloudTasksClient, "instances", [])
        monkeypatch.setattr("google.cloud.tasks_v2.CloudTasksClient", FakeCloudTasksClient)
        self.http = http_boundary_fake.install(monkeypatch)
        self.db = EvalDb()
        self.cipher = FailingDecrypt()
        self.store = FirestoreCredentialStore(self.db, ENV, cipher=self.cipher)
        self.email = FirestoreIndexedEmailRepository(self.db, ENV)
        self.task_config = FirestoreTaskConfigRepository(self.db, ENV)
        self.search_index = FirestoreTaskSearchIndex(self.db, ENV)
        self.queue = GcpTaskQueue("p", "us-central1", "worker-q", "https://svc.example.test")
        self.graph = GraphSubscriptions()
        self.task_setup = TaskSetupService(
            lifecycle=self.graph, task_config=self.task_config, tasks_provider=None, oauth_credentials=self.store,
            task_search_index=self.search_index, task_queue=self.queue,
            notification_url_base="https://svc.example.test", connector=ConnectorRegistry().get("microsoft_todo"))
        self.sessions = SessionService(secret_key=SECRET, access_token_ttl=3600, refresh_token_ttl=3600)
        registry = ConnectorRegistry(edges={"google": GoogleOAuthProvider("cid", "csecret"),
                                            "microsoft": MicrosoftOAuthProvider("ms-cid", "ms-csecret")})
        self.reconnect = create_autospec(IntegrationReconnectService, instance=True)
        # the drive (MULTI_DRIVE_RFC §4.4, §5.2): connections in the real service; the activation reads the grant's
        # drive identity through the real OneDriveAdapter behind the router
        self.drive_connections = DriveConnectionService(FakeDriveConnectionRepository(), self.store)
        onedrive = OneDriveAdapter(oauth_credentials=self.store, client_id="ms-cid", client_secret="ms-csecret",
                                   connector=registry.get("microsoft_onedrive"))
        self.drive_activation = build_drive_activation(
            connections=self.drive_connections, drive=DriveProviderRouter({"microsoft_onedrive": onedrive}),
            task_queue=None)
        connect_service = build_oauth_connect_service(
            self.db, SimpleNamespace(oauth_flows_collection="eval_oauth_flows"), connectors=registry,
            credentials=self.store, cipher=AesGcmTestCipher(), digest_key=b"k" * 32,
            revocation=CredentialRevocationService(self.store, registry.edges()),
            activations=build_connect_activations(
                reconnect=self.reconnect, todo_connector=registry.get("microsoft_todo"),
                task_queue=self.queue, drive_activation=self.drive_activation),
            redirect_uri=REDIRECT)
        self.app = Quart("test_reconnect_does_not_reindex")
        self.app.register_blueprint(create_oauth_blueprint(
            auth_service=Mock(), session_service=self.sessions, auth_registry=Mock(),
            auth_config=SimpleNamespace(oauth_redirect_uri="https://service.example/auth/callback"),
            connect_service=connect_service, connectors=registry))
        self._kind = "gmail"
        def token(tag: str) -> tuple:
            return 200, {"access_token": f"at-{tag}", "refresh_token": f"rt-{tag}", "expires_in": 3600,
                         "scope": GRANTED[self._kind]}

        self.http.route("POST", GOOGLE_TOKEN, handler=lambda r: token("g"))
        self.http.route("POST", MS_TOKEN, handler=lambda r: token("m"))
        self.http.route("GET", USERINFO, body={"sub": "sub-g", "email": "user@example.com"})
        self.http.route("GET", GRAPH_ME, body={"id": "sub-m", "mail": "user@example.test"})
        self.approot("d1")

    def approot(self, drive_id: str) -> None:
        self.http.route("GET", APPROOT, body={"id": "R!1", "parentReference": {"driveId": drive_id}})

    def bodies(self) -> List[dict]:
        return [b for c in FakeCloudTasksClient.instances for b in c.created_bodies()]

    def docs(self, name: str) -> dict:
        return {k: dict(v) for k, v in self.db.collection(name).docs.items()}

    async def consent(self, kind: str):
        """The user's connect of `kind` through the generic routes: the start, then the provider's callback."""
        self._kind = kind
        session = self.sessions.create_access_token(UserProfile(user_id=USER, account_id="acct-1"),
                                                    BillingAccount(account_id="acct-1"))
        async with self.app.test_client() as client:
            client.set_cookie("localhost", "access_token", session)
            start = await client.get(f"/auth/connect/{kind}")
            state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
            return await client.get(f"/auth/connect/callback?state={state}&code=c-1")


@pytest.fixture
def world(monkeypatch) -> World:
    return World(monkeypatch)


# ------------------------------------------------------------------------------------------------ Gmail

async def test_gmail_a_consent_writes_only_the_credential_and_enqueues_no_indexing(world):
    await world.email.update_indexing_state(IndexingState(user_id=USER, provider="gmail",
                                                          indexed_through=INDEXED_THROUGH,
                                                          oldest_indexed_through=OLDEST))
    world.db.collection(ENV.domain_email_facts_collection).docs["e1"] = {"user_id": USER, "subject": "Invoice FA-0137"}
    emails_before = world.docs(ENV.domain_email_facts_collection)
    assert await has_kind(world.store, USER, "gmail") is False

    resp = await world.consent("gmail")

    assert resp.headers["Location"] == "/cabinet?connected=gmail"
    assert [d["kind"] for d in world.docs(ENV.credentials_collection).values()] == ["gmail"]
    assert not [b for b in world.bodies() if b["task_type"] in ("email_indexing", "start_email_indexing")]
    state = await world.email.get_indexing_state(USER, "gmail")
    naive = (INDEXED_THROUGH.replace(tzinfo=None), OLDEST.replace(tzinfo=None))   # the repo reads naive UTC back
    assert (state.indexed_through.replace(tzinfo=None), state.oldest_indexed_through.replace(tzinfo=None)) == naive
    assert world.docs(ENV.domain_email_facts_collection) == emails_before
    assert world.http.unrouted == []


# ------------------------------------------------------------------------------------------------ Drive

class CredentialReadDrive:
    """The drive harness's scripted drive with the OneDrive adapter's credential read in front of the change feed: the
    adapter reads `get(user_id, credential_id)` from the store on every call (the decrypt runs there) and answers
    DriveNotConnectedError when the record is gone (onedrive_adapter.py). Everything else is the scripted drive."""

    def __init__(self, drive: Any, store: FirestoreCredentialStore) -> None:
        self._drive = drive
        self._store = store
        self.cursors: List[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._drive, name)

    async def get_changes(self, handle: Any, cursor: Any) -> Any:
        if await self._store.get(handle.user_id, handle.credential_id) is None:
            raise DriveNotConnectedError("no drive credential")
        self.cursors.append(cursor)
        return await self._drive.get_changes(handle, cursor)


class Drive:
    """The drive side (MULTI_DRIVE_RFC §4.4, §5.2, §8): a OneDrive consent goes through the generic routes and the real
    drive activation (the real OneDriveAdapter reads the App Folder root over the HTTP fake), which creates or
    reconnects the user's DriveConnection in the real DriveConnectionService; the indexer ticks that connection over the
    drive harness's scripted drive, reading the connection fresh from the same service."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.w = DriveWorld()
        self.w.drive.drive_identity = "microsoft_onedrive:d1"   # the identity the adapter builds for drive d1
        self.drive = CredentialReadDrive(self.w.drive, world.store)
        self.svc = DriveIndexingService(self.drive, self.w.repo, self.w.emb, self.w.queue, self.w.users,
                                        lambda lat, lon: None, "test-space/8",
                                        extractors={PARSER: FakeExtractor(PARSER), READER: FakeExtractor(READER)},
                                        triage=FakeTriage(), connections=world.drive_connections,
                                        clock=self.w.clock, monotonic=self.w.clock.monotonic)

    async def connection(self) -> Any:
        (found,) = await self.world.drive_connections.list_for_user(USER, include_disconnected=True)
        return found

    async def tick(self) -> Any:
        return await self.svc.tick((await self.connection()).scope)

    async def state(self) -> Any:
        return await self.w.repo.get_sync_state((await self.connection()).scope)

    async def settle(self, max_ticks: int = 50) -> Any:
        for _ in range(max_ticks):
            before = len(self.w.queue.tasks)
            result = await self.tick()
            continued = [t for t in self.w.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
            if not continued:
                return result
            self.w.clock.advance(max(1, max(t["delay"] for t in continued)))
        raise AssertionError("did not settle")

    async def built(self) -> Any:
        """Connected (one consent), indexing on, two files indexed."""
        resp = await _onedrive_consent(self.world)
        assert resp.headers["Location"] == "/cabinet?connected=microsoft_onedrive"
        conn = await self.connection()
        await self.world.drive_connections.set_indexing(USER, conn.connection_id, True)
        self.w.drive.add("a.pdf")
        self.w.drive.add("b.pdf")
        await self.settle()
        state = await self.state()
        assert state.drive_identity == "microsoft_onedrive:d1" and state.cursor and state.cursor.startswith("delta:")
        self.drive.cursors.clear()
        return state

    async def lose_the_grant(self) -> None:
        """The adapter's rejected refresh flags the connection (DriveReauthRequired -> flag_reconnect, §4.4)."""
        assert await self.world.drive_connections.flag_reconnect((await self.connection()).handle)


async def _onedrive_consent(world: World):
    return await world.consent("microsoft_onedrive")


async def test_drive_a_lost_grant_records_the_loss_and_keeps_the_index(world):
    drive = Drive(world)
    await drive.built()
    records = len(drive.w.repo.records)
    await drive.lose_the_grant()
    assert (await drive.tick()).status == "access_lost"
    assert (await drive.state()).access_lost_at is not None
    assert len(drive.w.repo.records) == records and drive.w.carded()


async def test_drive_no_credential_keeps_the_index_and_is_owed_the_reconnect_notice(world):
    """MULTI_DRIVE_RFC §4.4: an ACTIVE connection whose credential is gone is not a loss on the index (only a rejected
    grant flags the connection); the index is kept and R1 owes it the reconnect notice."""
    drive = Drive(world)
    await drive.built()
    records = dict(drive.w.repo.records)
    conn = await drive.connection()
    await world.store.delete(USER, conn.credential_id)
    await drive.tick()
    state = await drive.state()
    assert state.access_lost_at is None and not state.purging
    assert drive.w.repo.records == records
    assert [c.connection_id for c in await world.drive_connections.reconnect_needed(USER)] == [conn.connection_id]


async def test_drive_same_drive_consent_resumes_from_the_stored_cursor(world):
    drive = Drive(world)
    stored = await drive.built()
    records = dict(drive.w.repo.records)
    conn = await drive.connection()
    await drive.lose_the_grant()
    await drive.tick()
    assert (await drive.state()).access_lost_at is not None

    resp = await _onedrive_consent(world)
    assert resp.headers["Location"] == "/cabinet?connected=microsoft_onedrive"
    again = await drive.connection()
    assert (again.connection_id, again.drive_identity) == (conn.connection_id, "microsoft_onedrive:d1")
    result = await drive.tick()

    assert result.status not in ("purged", "purging", "access_lost")
    assert drive.drive.cursors and drive.drive.cursors[0] == stored.cursor
    after = await drive.state()
    assert after.access_lost_at is None and after.epoch == stored.epoch and not after.purging
    assert set(drive.w.repo.records) == set(records)


async def test_drive_another_drive_consent_purges_by_switch(world):
    drive = Drive(world)
    await drive.built()
    await drive.lose_the_grant()
    await drive.tick()
    world.approot("d2")

    await _onedrive_consent(world)
    assert (await drive.connection()).drive_identity == "microsoft_onedrive:d2"
    result = await drive.tick()

    assert result.status in ("purged", "purging")
    assert drive.w.repo.records == {}


async def test_drive_a_reconnect_past_the_grace_purges_as_expired(world):
    """Over 30 days after the loss: the index is purged (expired) and the next ticks rebuild it, a paid rebuild."""
    drive = Drive(world)
    await drive.built()
    await drive.lose_the_grant()
    await drive.tick()
    drive.w.clock.advance(ACCESS_LOSS_TTL.total_seconds() + 60)

    result = await drive.tick()

    assert result.status == "purged"
    assert drive.w.repo.records == {} and await drive.state() is None


async def test_drive_a_failing_decrypt_records_no_loss(world):
    """I4: a KMS failure on the drive's credential read is a failed tick, never "not connected": no loss mark, no
    purge. The tick decides from the stored connection (no decrypt, E28); the decrypt runs in the adapter's read."""
    drive = Drive(world)
    await drive.built()
    records = dict(drive.w.repo.records)
    world.cipher.failing = True

    result = await drive.tick()

    assert result.status not in ("purged", "purging", "access_lost")
    state = await drive.state()
    assert state.access_lost_at is None and not state.purging
    assert drive.w.repo.records == records
    assert (await drive.connection()).state is ConnectionState.ACTIVE
    assert await has_kind(world.store, USER, "microsoft_onedrive") is True


# ------------------------------------------------------------------------------------------------ To Do

async def _todo_consent_and_setup(world: World):
    """The consent, then the task it enqueued, delivered through the real WorkerHandler dispatch."""
    resp = await world.consent("microsoft_todo")
    assert resp.headers["Location"] == "/cabinet?connected=microsoft_todo"
    setups = [b for b in world.bodies() if b["task_type"] == "setup_microsoft_todo"]
    assert setups == [{"task_type": "setup_microsoft_todo", "user_id": USER}]
    worker = WorkerHandler(agent_worker_handler=None, email_indexing_service=None, notification_service=None,
                           consolidation_service=None, coordinator=None, agent_factory=None,
                           indexed_email_repo=None, user_repo=None, task_setup=world.task_setup)
    assert await worker.handle(setups[0]) == ({"status": "ok"}, 200)


async def _seed_todo(world: World, expires_at: datetime) -> dict:
    old = TaskSubscriptionConfig(sub_id="s0", list_id="l1", expires_at=expires_at)
    world.graph.live["s0"] = old
    await world.task_config.save_config(USER, TaskUserConfig(primary_list_id="l1", subscriptions=[old]))
    world.db.collection(ENV.task_search_index_collection).docs["u:t1"] = {"user_id": USER, "list_id": "l1",
                                                                          "title": "Renew the library card"}
    return world.docs(ENV.task_search_index_collection)


@pytest.mark.parametrize("prior", ["active", "lapsed"])
async def test_todo_a_consent_re_registers_once_and_re_reads_once_deleting_nothing(world, prior):
    """A consent is a new grant: a stored subscription, even one that still reads active, is not trusted (Graph may
    have dropped it with the old consent), so changes made during the gap are read once."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    index = await _seed_todo(world, now + timedelta(days=2) if prior == "active" else now - timedelta(days=1))

    await _todo_consent_and_setup(world)

    reindex = [b for b in world.bodies() if b["task_type"] == "reindex_task_list"]
    assert reindex == [{"task_type": "reindex_task_list", "user_id": USER, "list_id": "l1"}]
    assert world.graph.registered == ["s1"] and set(world.graph.live) == {"s1"}
    config = await world.task_config.get_config(USER)
    assert config.primary_list_id == "l1" and [s.sub_id for s in config.subscriptions] == ["s1"]
    assert world.docs(ENV.task_search_index_collection) == index


async def test_todo_the_renewal_path_still_keeps_an_active_subscription(world):
    """Only the consent re-subscribes: after it, the sweep's ensure_subscriptions keeps the live one, reads nothing."""
    await _seed_todo(world, datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1))
    await _todo_consent_and_setup(world)
    before = len(world.bodies())

    await world.task_setup.ensure_subscriptions(USER)

    assert world.graph.registered == ["s1"] and world.bodies()[before:] == []
    assert [s.sub_id for s in (await world.task_config.get_config(USER)).subscriptions] == ["s1"]
