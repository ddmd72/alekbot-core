"""P13 card E: the two Smart-bound background runs (`execute_reminder`, `daily_email_review`) through the real
`UserNotificationService` and the real coordinator, and the two Scheduler fan-outs that create their tasks.

What the agent is handed (budget, labels, payload), what history keeps (and only that), where the review's page is
filed, and the deadline and name each fan-out gives its task. Tests 1-3 run on the seam world (scripted agents), 4-5 on
the redelivery world (real `WorkerHandler`, `RemindersService`, `TaskDispatchService` and queue). Pins P13-D2.
"""
import hashlib
import json
from datetime import timedelta

import pytest

from src.domain.complexity_settings import ComplexitySettings
from src.domain.notification_kind import NotificationKind
from src.domain.task_complexity import TaskComplexity
from src.domain.user import PerformanceTier, UserBotConfig, UserProfile
from src.domain.workspace import fire_key
from src.handlers import worker_handler
from src.infrastructure.notification_sla import NOTIFICATION_SLA, dispatch_deadline_s
from src.services import reminders_service
from src.services.email_review_service import EmailReviewService
from src.services.reminders_service import build_reminder_alert
from tests.support.clock_instants import pin_datetime
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.support.redelivery_world import RedeliveryWorld, ScriptedReview, deliver
from tests.support.seam_world import (
    ACCOUNT, OTHER_SLACK, PRIMARY, T0, USER, Delegation, SeamWorld, Step, page, require, says,
)
from src.composition.connector_registry import ConnectorRegistry

CREATED = T0 - timedelta(days=1)
EMAILS = [
    {"email_id": "m1", "from": "user@example.com", "subject": "Your flight booking AAA→BBB",
     "date": "2026-10-26T18:00:00+00:00", "snippet": "", "body": "", "attachments": []},
    {"email_id": "m2", "from": "user@example.com", "subject": "Example School parents' meeting",
     "date": "2026-10-26T19:00:00+00:00", "snippet": "", "body": "", "attachments": []},
]
D2 = ("P13-D2: a background run's document follows the last-active channel at delivery, not the channel the run "
      "spoke in; fix after the plan (owner)")


def fire(note_id: str) -> dict:
    return {"task_type": "execute_reminder", "note_id": note_id, "user_id": USER, "due_at": T0.isoformat()}


# ----------------------------------------------------------------------------- 1. the reminder fire

@pytest.mark.parametrize("complexity,override", [
    pytest.param(TaskComplexity.DEEP_REASONING, None, id="deep-reasoning-by-default"),
    pytest.param(TaskComplexity.SIMPLE_ANALYTICS, PerformanceTier.PERFORMANCE, id="user-override-to-performance"),
])
async def test_a_reminder_fire_reaches_smart_with_its_budget_and_labels_and_keeps_its_history(
        monkeypatch, complexity, override):
    """Guards: alert `worker_handler.py:1009` → `ums.py:458` (the user turn's `full_text`), summary `:1010` →
    `ums.py:457` (its `text`); tier `:1012` → `ums.py:345-348,403`
    (the budget, 2026-08-15 incident); recipient `:1013` → `ums.py:375`; `task_complexity` `:1014` → `:390`;
    `suppress_transient_retry` `:1017` → `:397`; `origin_detail` `:1020` → `:400`; `fire_key` `:1021` → `:401`;
    response summary `ums.py:445-446,460` (the model turn)."""
    world = SeamWorld(monkeypatch)
    if override is not None:
        world.users.put(UserProfile(user_id=USER, account_id=ACCOUNT, config=UserBotConfig(
            timezone="Europe/Berlin",
            complexity_settings_overrides={complexity: ComplexitySettings(tier=override)})))
    await world.set_primary(*PRIMARY)
    smart = world.agent("smart_response_agent").play(
        says("Time to call dad.", summary="Reminded the user to call dad."))
    quick = world.agent("quick_response_agent")
    note_id = await world.seed_note(monkeypatch, due=T0, created_at=CREATED, complexity=complexity)
    note = await world.notes.get_note(user_id=USER, note_id=note_id)   # the fire deletes a one-time note
    require(note is not None and note.complexity == complexity, "the note is stored with its complexity")

    answer = await world.run(fire(note_id))

    require(not world.unscripted(), "every agent call was scripted")
    require(answer == ({"status": "ok"}, 200), "the fire was delivered")
    assert [p.value for p in world.posts_to(*PRIMARY)] == ["Time to call dad."]
    assert quick.received == []
    [message] = smart.received
    assert message.timeout_ms == NOTIFICATION_SLA[NotificationKind.REMINDER].tier_overrides[PerformanceTier.PERFORMANCE]
    assert message.context["task_complexity"] == complexity.value
    assert message.context["run_origin"] == "background:execute_reminder"
    assert message.context["run_fire_key"] == fire_key(note_id, T0)
    assert message.context["suppress_transient_retry"] is True
    assert message.context["session_id"] == "u1:C1"
    assert world.history("u1:C1") == [
        ("user", 'Received reminder: "call dad"', build_reminder_alert(note)),
        ("model", "Reminded the user to call dad.", "Time to call dad."),
    ]


# ----------------------------------------------------------------------------- 2. the daily review

async def test_a_daily_review_reaches_smart_with_its_payload_writes_no_history_and_files_its_page_as_an_email_review(
        monkeypatch):
    """Guards: recipient `worker_handler.py:1199` → `ums.py:375`; `save_history=False` `:1200` → `:443`;
    `framing_suffix=""` `:1201` → `:377`; `thinking_effort`/`task_complexity`/`email_for_triage` `:1202-1204` →
    `:389-391`; `storage_class` `:1207` → `:394` → (coordinator spread `agent_coordinator.py:562`) →
    `html_page_generator_agent.py:187` → `agent_worker_handler.py:271` → `document_delivery_service.py:87`;
    `suppress_transient_retry` `:1210` → `:397`; `origin_detail` `:1211` → `:400`."""
    world = SeamWorld(monkeypatch, email_review=ScriptedReview(EMAILS))
    await world.set_primary(*PRIMARY)
    pin_datetime(monkeypatch, worker_handler, T0)   # the alert's date is read from real time (worker_handler.py:1190)
    smart = world.agent("smart_response_agent").play(
        Step(says("Your inbox review is ready."), Delegation("create_html_page", "The inbox review")))

    answer = await world.run({"task_type": "daily_email_review", "user_id": USER, "account_id": ACCOUNT})

    require(not world.unscripted(), "every agent call was scripted")
    require(answer == ({"status": "ok", "emails": 2}, 200), "the review was delivered")
    assert [(p.kind, p.value) for p in world.posts_to(*PRIMARY)] == [("text", "Your inbox review is ready.")]
    [message] = smart.received
    assert message.timeout_ms == NOTIFICATION_SLA[NotificationKind.DAILY_DIGEST].timeout_ms
    assert [p.text for p in message.context["current_message_parts"]] == [
        f"[System: {EmailReviewService.build_alert('2026-10-27', 2)}]"]
    assert message.context["thinking_effort"] == "medium"
    assert message.context["task_complexity"] == "deep_reasoning"
    assert message.context["email_for_triage"] == EMAILS
    assert message.context["storage_class"] == "email_review"
    assert message.context["run_origin"] == "background:daily_email_review"
    assert message.context["suppress_transient_retry"] is True
    assert "run_fire_key" not in message.context
    assert world.history("u1:C1") == []
    [body] = world.bodies("agent_execution")
    assert body["context"]["storage_class"] == "email_review"

    world.agent("html_page_generator_agent").play(page("inbox-review.html", "Inbox review.html"))
    await world.run(body)

    require(not world.unscripted(), "every agent call was scripted")
    [key] = world.stored_keys()
    assert key.startswith("email_review/u1/")
    [(_, user_text, _), (_, note, note_full)] = world.history("u1:C1")
    assert user_text == "[System: async document ready — Inbox review.html]"
    assert note == note_full and f'"file_ref": "{key}"' in note


# ----------------------------------------------------------------------------- 3. where a reminder's page lands

async def _reminder_with_a_page(monkeypatch, world: SeamWorld) -> dict:
    """A one-time "call dad" fire whose Smart turn asks for a page; returns the created `agent_execution` body."""
    world.agent("smart_response_agent").play(
        Step(says("Time to call dad."), Delegation("create_html_page", "A call checklist")))
    world.agent("html_page_generator_agent").play(page("call-checklist.html", "Call checklist.html"))
    note_id = await world.seed_note(monkeypatch, due=T0, created_at=CREATED)

    answer = await world.run(fire(note_id))

    require(not world.unscripted(), "every agent call was scripted")
    require(answer == ({"status": "ok"}, 200), "the fire was delivered")
    require([p.value for p in world.posts_to(*PRIMARY)] == ["Time to call dad."], "the reminder spoke on C1")
    bodies = world.bodies("agent_execution")
    require(len(bodies) == 1, "the turn created exactly one agent_execution task")
    return bodies[0]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=D2)
async def test_a_reminder_page_lands_where_the_reminder_spoke(monkeypatch):
    """No primary: the reminder speaks on the last-active channel (C1); by the time its page is delivered the user has
    chatted in C2. The run's context carries no origin (`ums.py:383-402`), so the page follows the last-active
    channel at delivery (`agent_worker_handler.py:258-259` → `ums.py:504-506,136-137`).
    Flipping fix: add the resolved channel to the context at `ums.py:383-402` (`"origin_channel_id":
    channel_info.channel_id, "origin_platform": channel_info.platform`)."""
    world = SeamWorld(monkeypatch)
    await world.set_last_active(*PRIMARY)
    body = await _reminder_with_a_page(monkeypatch, world)
    await world.set_last_active(*OTHER_SLACK)   # the user chatted elsewhere

    await world.run(body)

    require(not world.unscripted(), "every agent call was scripted")
    docs = [p for p in world.channels.posts if p.kind == "doc"]
    require(len(docs) == 1, "exactly one document link was posted")
    assert (docs[0].platform, docs[0].channel_id) == PRIMARY


async def test_with_a_primary_set_a_reminder_page_stays_on_the_primary(monkeypatch):
    """Control of the P13-D2 pin: with a primary the page lands there today (`ums.py:129-131`) and after the fix (the
    override). Guards `agent_worker_handler.py:258-259` → `ums.py:504-506`."""
    world = SeamWorld(monkeypatch)
    await world.set_primary(*PRIMARY)
    body = await _reminder_with_a_page(monkeypatch, world)
    await world.set_last_active(*OTHER_SLACK)

    await world.run(body)

    require(not world.unscripted(), "every agent call was scripted")
    docs = [p for p in world.channels.posts if p.kind == "doc"]
    require(len(docs) == 1, "exactly one document link was posted")
    assert (docs[0].platform, docs[0].channel_id) == PRIMARY
    assert world.posts_to(*OTHER_SLACK) == []


# ----------------------------------------------------------------------------- 4-5. the Scheduler fan-outs

async def test_a_reminder_fire_is_enqueued_with_the_reminder_deadline(monkeypatch):
    """Guards: `reminders_service.py:183` (`deadline_seconds`) → `tds:85` → `gcp:391-392` (`dispatch_deadline`)."""
    world = RedeliveryWorld()
    await world.seed_note(monkeypatch, due=T0, created_at=CREATED)
    pin_datetime(monkeypatch, reminders_service, T0 + timedelta(seconds=30))

    answer = await deliver(world.handler, {"task_type": "fire_due_reminders"})

    require(answer[1] == 200, "the tick was answered 200")
    [task] = [r["task"] for r in world.client.create_task_requests
              if json.loads(r["task"]["http_request"]["body"])["task_type"] == "execute_reminder"]
    assert task["dispatch_deadline"].seconds == dispatch_deadline_s(NotificationKind.REMINDER)


async def test_a_daily_review_is_enqueued_named_by_its_mark_with_the_digest_deadline(monkeypatch):
    """Guards: `worker_handler.py:1139` (`deadline_seconds`), `:1141` (`dedup_key`) → `tds:85-86` →
    `gcp:380-384` (name), `:391-392` (`dispatch_deadline`)."""
    oauth = CredentialsStore()
    world = RedeliveryWorld(
        email_review=EmailReviewService(email_provider=None, oauth_credentials=oauth.repo, connector=ConnectorRegistry().get("gmail")),  # type: ignore[arg-type]
    )
    await oauth.repo.save(creds(USER, "gmail"))
    world.users.put(UserProfile(user_id=USER, account_id=ACCOUNT, config=UserBotConfig(
        timezone="Europe/Berlin", gmail_daily_review=True, gmail_daily_review_hour=9)))
    pin_datetime(monkeypatch, worker_handler, T0)   # 09:00 in Berlin: inside the review window

    answer = await deliver(world.handler, {"task_type": "start_daily_email_review"})

    require(answer == ({"started": 1, "failed": 0}, 200), "one review was started")
    [task] = [r["task"] for r in world.client.create_task_requests
              if json.loads(r["task"]["http_request"]["body"])["task_type"] == "daily_email_review"]
    digest = hashlib.sha256(b"daily_review:u1:2026-10-27").hexdigest()[:32]
    assert task["name"].endswith("/daily_email_review-" + digest)
    assert task["dispatch_deadline"].seconds == dispatch_deadline_s(NotificationKind.DAILY_DIGEST)
