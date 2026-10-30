"""IntegrationReconnectService (SECRETS_AT_REST_RFC §6.2 R1, F1, F2; §8 "R1"; plan D-P4, D-P5) over the real stores on one
evaluating Firestore fake: the credential adapter, the Gmail indexing-state repository and the To Do config repository;
the drive side on the in-memory drive index repository and a drive whose connection the test sets. The notifier is the
`notify` boundary, scripted with `NotifyResult`s. Assertions are on what the stores hold and what was sent."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.composition.connector_registry import ConnectorRegistry
from src.domain.agent import AgentStatus
from src.domain.drive_index import DriveSyncState
from src.domain.email import IndexingState
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.notification_kind import NotificationKind
from src.domain.notify_result import NotifyResult
from src.domain.reconnect_policy import R1_NOTICES, Integration
from src.domain.task import TaskUserConfig
from src.domain.user_drive import DriveConnection
from src.services.integration_reconnect_service import IntegrationReconnectService
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import delete_kind

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
NOW = datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)
CREDS, FACTS, STATE, TASKS = "eval_credentials", "eval_email_facts", "eval_email_state", "eval_task_config"


class Notifier:
    """The `notify` boundary: records each call; answers `result`, or raises `raises`."""

    def __init__(self) -> None:
        self.calls: List[dict] = []
        self.result = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=True)
        self.raises: Optional[Exception] = None

    async def notify(self, user_id, account_id, system_alert, *, kind, **_):
        self.calls.append({"user_id": user_id, "account_id": account_id, "alert": system_alert, "kind": kind})
        if self.raises is not None:
            raise self.raises
        return self.result


class Users:
    def __init__(self, *user_ids: str) -> None:
        self.ids = set(user_ids)

    async def get_user(self, user_id):
        return SimpleNamespace(user_id=user_id, account_id=f"acc-{user_id}") if user_id in self.ids else None


class Drive:
    def __init__(self) -> None:
        self.per_user: Dict[str, DriveConnection] = {}

    async def connection(self, user_id):
        return self.per_user.get(user_id, DriveConnection(connected=False))


class Alerts:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text):
        self.posts.append(text)


class World:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={CREDS: "development_credentials",
                                                        FACTS: "development_domain_email_facts_v1",
                                                        STATE: "development_email_indexing_state"})
        env = SimpleNamespace(credentials_collection=CREDS, domain_email_facts_collection=FACTS,
                              email_indexing_state_collection=STATE, task_config_collection=TASKS)
        self.creds = FirestoreCredentialStore(self.db, env, cipher=AesGcmTestCipher())
        self.email = FirestoreIndexedEmailRepository(self.db, env)
        self.tasks = FirestoreTaskConfigRepository(self.db, env)
        self.drive_states = InMemoryDriveIndexRepository()
        self.drive = Drive()
        self.notifier = Notifier()
        self.alerts = Alerts()
        self.ensured: List[str] = []

        async def ensure(user_id):
            self.ensured.append(user_id)

        self.svc = IntegrationReconnectService(
            credentials=self.creds, users=Users("user-a", "user-b"), notifier=self.notifier,
            email_states=self.email, task_config=self.tasks, drive_states=self.drive_states, drive=self.drive,
            alert_sink=self.alerts, ensure_agents=ensure, clock=lambda: NOW, mail_connector=ConnectorRegistry().get("gmail"), todo_connector=ConnectorRegistry().get("microsoft_todo"))

    async def connect(self, user_id: str, provider: str) -> None:
        await self.creds.save(OAuthCredential(
            user_id=user_id, account_id="acct-1", kind=provider,
            integration=ConnectorRegistry().get(provider).integration,
            account=ProviderAccount(realm="microsoft" if provider.startswith("microsoft") else "google",
                                    subject=f"sub-{user_id}", label="user@example.com"),
            access_token="at", refresh_token="rt", token_expiry=NOW, scopes=[]))

    async def gmail_state(self, user_id: str) -> None:
        await self.email.update_indexing_state(IndexingState(user_id=user_id, provider="gmail",
                                                             indexed_through=datetime(2026, 10, 1)))

    async def todo_config(self, user_id: str) -> None:
        await self.tasks.save_config(user_id, TaskUserConfig(primary_list_id="list-1"))

    def alerts_sent(self) -> List[str]:
        return [c["alert"] for c in self.notifier.calls]


@pytest.fixture
def world():
    return World()


# ----------------------------------------------------------------------------- the sweep

async def test_a_past_connection_without_a_credential_gets_one_notice_per_integration(world):
    await world.gmail_state("user-a")
    await world.todo_config("user-a")
    summary = await world.svc.sweep()
    assert summary == {"candidates": 2, "notices": 2, "errors": 0}
    assert world.notifier.calls == [
        {"user_id": "user-a", "account_id": "acc-user-a", "alert": R1_NOTICES[Integration.GMAIL],
         "kind": NotificationKind.DOCUMENT_DELIVERY},
        {"user_id": "user-a", "account_id": "acc-user-a", "alert": R1_NOTICES[Integration.TODO],
         "kind": NotificationKind.DOCUMENT_DELIVERY},
    ]
    assert world.ensured == ["user-a", "user-a"]
    # the flag lives on the feature state (F2)
    assert world.db.collection(STATE).docs["user-a_gmail"]["reconnect_notified"] is True
    assert world.db.collection(TASKS).docs["user-a"]["reconnect_notified"] is True
    assert world.db.collection(TASKS).docs["user-a"]["primary_list_id"] == "list-1"


async def test_the_next_sweep_sends_none(world):
    await world.gmail_state("user-a")
    await world.todo_config("user-a")
    await world.svc.sweep()
    summary = await world.svc.sweep()
    assert summary["notices"] == 0 and len(world.notifier.calls) == 2


async def test_a_credential_means_no_notice(world):
    await world.gmail_state("user-a")
    await world.todo_config("user-a")
    await world.connect("user-a", "gmail")
    await world.connect("user-a", "microsoft_todo")
    assert (await world.svc.sweep())["notices"] == 0
    assert world.notifier.calls == []


async def test_a_consent_clears_the_flag_and_the_next_loss_notifies_again(world):
    await world.gmail_state("user-a")
    await world.todo_config("user-a")
    await world.svc.sweep()
    await world.connect("user-a", "gmail")
    await world.connect("user-a", "microsoft_todo")
    await world.svc.consented("user-a", Integration.GMAIL)
    await world.svc.consented("user-a", Integration.TODO)
    assert (await world.email.get_indexing_state("user-a", "gmail")).reconnect_notified is False
    assert (await world.tasks.get_config("user-a")).reconnect_notified is False
    await delete_kind(world.creds, "user-a", "gmail")
    await delete_kind(world.creds, "user-a", "microsoft_todo")
    assert (await world.svc.sweep())["notices"] == 2
    assert len(world.notifier.calls) == 4


async def test_a_credential_that_is_back_lowers_the_flag_without_a_consent(world):
    await world.gmail_state("user-a")
    await world.todo_config("user-a")
    await world.svc.sweep()
    await world.connect("user-a", "gmail")
    await world.connect("user-a", "microsoft_todo")
    await world.svc.sweep()
    assert (await world.email.get_indexing_state("user-a", "gmail")).reconnect_notified is False
    assert (await world.tasks.get_config("user-a")).reconnect_notified is False
    assert len(world.notifier.calls) == 2


async def test_a_deliberate_gmail_disconnect_sends_none_and_a_consent_clears_the_mark(world):
    await world.gmail_state("user-a")
    await world.svc.mark_disconnected("user-a", Integration.GMAIL)
    assert world.db.collection(STATE).docs["user-a_gmail"]["disconnected_by_user_at"] == NOW
    assert (await world.svc.sweep())["notices"] == 0
    assert world.notifier.calls == []
    assert await world.svc.status("user-a", (Integration.GMAIL,)) == {"gmail": False}
    await world.svc.consented("user-a", Integration.GMAIL)
    assert (await world.email.get_indexing_state("user-a", "gmail")).disconnected_by_user_at is None


async def test_a_reset_to_do_config_sends_none(world):
    """D-P5: To Do's disconnect stores an empty config, so no past connection is left for R1 to see."""
    await world.tasks.save_config("user-a", TaskUserConfig())
    summary = await world.svc.sweep()
    assert summary == {"candidates": 1, "notices": 0, "errors": 0}
    assert world.notifier.calls == []


async def test_to_do_has_no_disconnect_mark(world):
    with pytest.raises(ValueError):
        await world.svc.mark_disconnected("user-a", Integration.TODO)


async def test_notify_raising_leaves_the_flag_down_and_the_next_sweep_retries(world):
    await world.gmail_state("user-a")
    world.notifier.raises = RuntimeError("channel lookup failed")
    summary = await world.svc.sweep()
    assert summary == {"candidates": 1, "notices": 0, "errors": 1}
    assert (await world.email.get_indexing_state("user-a", "gmail")).reconnect_notified is False
    world.notifier.raises = None
    assert (await world.svc.sweep())["notices"] == 1
    assert (await world.email.get_indexing_state("user-a", "gmail")).reconnect_notified is True


@pytest.mark.parametrize("status", [AgentStatus.FAILED, AgentStatus.TIMEOUT])
async def test_nothing_sent_lowers_the_flag_and_is_retried(world, status):
    await world.todo_config("user-a")
    world.notifier.result = NotifyResult(delivered=False, agent_status=status, error="formatter")
    await world.svc.sweep()
    assert (await world.tasks.get_config("user-a")).reconnect_notified is False
    world.notifier.result = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=True)
    await world.svc.sweep()
    assert len(world.notifier.calls) == 2
    assert (await world.tasks.get_config("user-a")).reconnect_notified is True


async def test_an_attempted_send_is_not_repeated(world):
    await world.gmail_state("user-a")
    world.notifier.result = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="slack down",
                                         sent=True)
    await world.svc.sweep()
    await world.svc.sweep()
    assert len(world.notifier.calls) == 1
    assert (await world.email.get_indexing_state("user-a", "gmail")).reconnect_notified is True


async def test_an_empty_answer_is_final_and_alerted_once(world):
    await world.gmail_state("user-a")
    world.notifier.result = NotifyResult(delivered=False, agent_status=AgentStatus.SUCCESS, error="empty_answer")
    await world.svc.sweep()
    await world.svc.sweep()
    assert len(world.notifier.calls) == 1
    assert len(world.alerts.posts) == 1
    assert (await world.email.get_indexing_state("user-a", "gmail")).reconnect_notified is True


async def test_one_failing_user_does_not_stop_the_rest(world):
    await world.gmail_state("user-a")
    await world.gmail_state("user-b")
    calls = 0
    real = world.notifier.notify

    async def first_raises(user_id, account_id, system_alert, *, kind, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return await real(user_id, account_id, system_alert, kind=kind, **kw)

    world.notifier.notify = first_raises
    summary = await world.svc.sweep()
    assert summary == {"candidates": 2, "notices": 1, "errors": 1}


async def test_a_user_without_a_profile_is_skipped_with_the_flag_down(world):
    await world.gmail_state("user-z")
    assert (await world.svc.sweep())["notices"] == 0
    assert world.notifier.calls == []
    assert (await world.email.get_indexing_state("user-z", "gmail")).reconnect_notified is False


# ----------------------------------------------------------------------------- status and the drive mark

async def test_status_reads_reconnect_per_integration(world):
    await world.gmail_state("user-a")
    await world.todo_config("user-a")
    await world.drive_states.save_sync_state(DriveSyncState(user_id="user-a"))
    assert await world.svc.status("user-a") == {"gmail": True, "drive": True, "todo": True}
    await world.connect("user-a", "gmail")
    world.drive.per_user["user-a"] = DriveConnection(connected=True, drive_id="d1")
    assert await world.svc.status("user-a") == {"gmail": False, "drive": False, "todo": True}
    # nothing stored: never connected, nothing to reconnect
    assert await world.svc.status("user-b") == {"gmail": False, "drive": False, "todo": False}


async def test_a_drive_disconnect_marks_the_sync_state_and_a_consent_clears_it(world):
    await world.drive_states.save_sync_state(DriveSyncState(user_id="user-a"))
    await world.svc.mark_disconnected("user-a", Integration.DRIVE)
    assert (await world.drive_states.get_sync_state("user-a")).disconnected_by_user_at == NOW
    assert await world.svc.status("user-a", (Integration.DRIVE,)) == {"drive": False}
    await world.svc.consented("user-a", Integration.DRIVE)
    assert (await world.drive_states.get_sync_state("user-a")).disconnected_by_user_at is None
    assert await world.svc.status("user-a", (Integration.DRIVE,)) == {"drive": True}


async def test_the_sweep_never_notifies_for_the_drive(world):
    """Drive's R1 notice is the drive fan-out's (W2.3), not the sweep's."""
    await world.drive_states.save_sync_state(DriveSyncState(user_id="user-a"))
    assert await world.svc.sweep() == {"candidates": 0, "notices": 0, "errors": 0}
