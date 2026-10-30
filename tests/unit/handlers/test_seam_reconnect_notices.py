"""R1's sweep from `/worker` (SECRETS_AT_REST_RFC §6.2; plan D-P4): the Scheduler body `reconnect_notice_sweep` reaches
`IntegrationReconnectService.sweep` through the real `WorkerHandler`, and its notice goes through the real
`UserNotificationService` and coordinator to the user's PRIMARY channel, never the last active one (a sweep has no
origin), with the history pair in the primary's session.

On `SeamWorld` (tests/support/seam_world.py): the primary is `("slack", "C1")`, the last active channel is
`("telegram", "7001")`, so a notice routed by the last active channel lands on Telegram and the assertions on C1 catch
it. The stores are the real Firestore adapters on the world's `EvalDb`; the quick formatter is a scripted stand-in."""
from datetime import datetime, timezone

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.domain.email import IndexingState
from src.domain.reconnect_policy import R1_NOTICES, Integration
from src.services.integration_reconnect_service import IntegrationReconnectService
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.redelivery_world import deliver
from tests.support.seam_world import PRIMARY, TELEGRAM, USER, SeamWorld, require, says
from src.composition.connector_registry import ConnectorRegistry

NOW = datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)
TEXT = "Your Gmail connection needs to be made again: Cabinet, Gmail, Reconnect."


def _world(monkeypatch):
    """The world plus the production service on its `EvalDb`, as `build_application_graph` wires it (its stores, the
    world's notifier, user repository and agent loader). The service needs the world's db, which exists only once the
    world is built, so it is attached to the built handler (the `reconnect=` keyword's attribute)."""
    world = SeamWorld(monkeypatch)
    email = FirestoreIndexedEmailRepository(world.db, world.env)
    world.handler._reconnect = IntegrationReconnectService(
        credentials=FirestoreCredentialStore(world.db, world.env, cipher=AesGcmTestCipher()), users=world.users, notifier=world.notifier,
        email_states=email, task_config=FirestoreTaskConfigRepository(world.db, world.env),
        ensure_agents=world.factory.ensure_agents_for_user, clock=lambda: NOW, mail_connector=ConnectorRegistry().get("gmail"), todo_connector=ConnectorRegistry().get("microsoft_todo"))
    return world, email


def _alert_of(message) -> str:
    return message.context["current_message_parts"][0].text


async def test_the_sweep_notice_lands_on_the_primary_not_the_last_active_channel(monkeypatch):
    world, email = _world(monkeypatch)
    await world.set_primary(*PRIMARY)
    await world.set_last_active(*TELEGRAM)
    await email.update_indexing_state(IndexingState(user_id=USER, provider="gmail"))
    quick = world.agent("quick_response_agent").play(says(TEXT))

    answer = await deliver(world.handler, {"task_type": "reconnect_notice_sweep"})

    require(not world.unscripted(), "every agent call was scripted")
    assert answer == ({"candidates": 1, "notices": 1, "errors": 0}, 200)
    assert [(p.kind, p.value) for p in world.posts_to(*PRIMARY)] == [("text", TEXT)]
    assert world.posts_to(*TELEGRAM) == []
    assert R1_NOTICES[Integration.MAIL] in _alert_of(quick.received[0])
    assert [role for role, _, _ in world.history(f"{USER}:C1")] == ["user", "model"]
    assert (await email.get_indexing_state(USER, "gmail")).reconnect_notified is True


async def test_a_second_sweep_posts_nothing(monkeypatch):
    world, email = _world(monkeypatch)
    await world.set_primary(*PRIMARY)
    await email.update_indexing_state(IndexingState(user_id=USER, provider="gmail"))
    world.agent("quick_response_agent").play(says(TEXT))

    await deliver(world.handler, {"task_type": "reconnect_notice_sweep"})
    answer = await deliver(world.handler, {"task_type": "reconnect_notice_sweep"})

    require(not world.unscripted(), "every agent call was scripted")
    assert answer == ({"candidates": 1, "notices": 0, "errors": 0}, 200)
    assert len(world.posts_to(*PRIMARY)) == 1


async def test_unconfigured_answers_disabled(monkeypatch):
    world = SeamWorld(monkeypatch)
    assert await deliver(world.handler, {"task_type": "reconnect_notice_sweep"}) == ({"status": "disabled"}, 200)
