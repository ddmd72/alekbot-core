"""P13 card C: the async document chain through `AgentWorkerHandler` on the seam world.

Every result an `agent_execution` task delivers (page, PDF, image, DOCX, failure notice) and the deep-research report
chain reach the channel in the task's context, store under the right prefix, and leave the history note in that
channel's session. The primary channel is ("slack", "C1") in every test, so a dropped origin lands somewhere visibly
wrong. Bodies are built as the coordinator creates them (`agent_coordinator.py:558-564` → `gcp_task_queue.py:207-213`)
and delivered through `/worker` with `world.run`.

Not here (P13 inventory §4): the raw report link (no live path posts it), the `execute_deep_research_claude` branch
(no producer), the drive copy line.
"""
import hashlib
from types import SimpleNamespace

import pytest

from src.domain.deep_research_drive_copy import DEEP_RESEARCH_REPORT_CONTEXT_KEY, marker_query
from src.domain.deep_research_notices import report_not_prepared_notice
from src.services import deep_research_delivery
from tests.support.redelivery_world import Post
from tests.support.seam_world import (
    ACCOUNT, OTHER_SLACK, PRIMARY, TELEGRAM, USER, SeamWorld, docx, fails, page, require, says,
)

QUERY = "Heat pumps in Springfield"


@pytest.fixture
async def world(monkeypatch):
    w = SeamWorld(monkeypatch)
    await w.set_primary(*PRIMARY)
    return w


def body(intent: str, agent_id: str, origin, *, query: str = "q", **context):
    """An `agent_execution` body as the coordinator creates it, from a chat in `origin` = (platform, channel)."""
    platform, channel = origin
    return {"task_type": "agent_execution", "agent_id": agent_id, "intent": intent, "query": query,
            "context": {"user_id": USER, "account_id": ACCOUNT, "session_id": f"{USER}:{channel}",
                        "origin_channel_id": channel, "origin_platform": platform, "params": {}, **context}}


def document_note(history, label):
    """The key the document note of `label` names, or None when the session has no such pair."""
    for (role, text, _), (next_role, note, full) in zip(history, history[1:]):
        if role == "user" and text == f"[System: async document ready — {label}]" and next_role == "model":
            if note == full and '"file_ref": "' in note:
                return note.split('"file_ref": "', 1)[1].split('"', 1)[0]
    return None


GENERATORS = [
    pytest.param("create_html_page", "html_page_generator_agent", "heat-pumps.html",
                 "Heat pumps in Springfield.html", "text/html; charset=utf-8", id="page"),
    pytest.param("create_pdf", "pdf_generator_agent", "heat-pumps.pdf",
                 "Heat pumps in Springfield.pdf", "application/pdf", id="pdf"),
    pytest.param("generate_image", "image_generation_agent", "heat-pump.png",
                 "Heat pump in Springfield.png", "image/png", id="image"),
]


@pytest.mark.parametrize("intent,agent_id,filename,label,content_type", GENERATORS)
async def test_a_generated_document_reaches_the_chat_it_was_asked_in(world, intent, agent_id, filename, label,
                                                                     content_type):
    """Guards: origin `agent_worker_handler.py:258-259,281` → `ums.py:504-506` (`resolve_channel` override); key
    `agent_worker_handler.py:280` → `ums.py:537-544` (the note names the stored key); session `ums.py:550`."""
    world.agent(agent_id).play(page(filename, label, content_type=content_type))

    answer = await world.run(body(intent, agent_id, TELEGRAM))

    require(not world.unscripted(), "every agent call was scripted")
    require(answer == ({"status": "success", "agent_id": f"{agent_id}_{USER}", "intent": intent}, 200),
            "the task succeeded")
    [(posted_label, url)] = world.docs_to(*TELEGRAM)
    assert posted_label == label and url.startswith("https://svc.test.example/f/")
    assert world.posts_to(*PRIMARY) == []
    [key] = world.stored_keys()
    assert key.startswith("docs/u1/") and key.endswith("-" + filename)
    history = world.history("u1:7001")
    assert [role for role, _, _ in history] == ["user", "model"]
    assert document_note(history, label) == key
    assert world.history("u1:C1") == []


async def test_an_email_review_page_is_stored_under_the_gated_prefix(world):
    """Guards: `agent_worker_handler.py:271` (`storage_class` of the item) → `document_delivery_service.py:87` (the
    prefix); the item's value is the agent's own read of the run context (`html_page_generator_agent.py:187`), which
    the handler passes on (`agent_worker_handler.py:118`)."""
    world.agent("html_page_generator_agent").play(page("inbox-review.html", "Inbox review.html"))

    await world.run(body("create_html_page", "html_page_generator_agent", PRIMARY, storage_class="email_review"))

    require(not world.unscripted(), "every agent call was scripted")
    [key] = world.stored_keys()
    assert key.startswith("email_review/u1/")
    assert [label for label, _ in world.docs_to(*PRIMARY)] == ["Inbox review.html"]
    assert document_note(world.history("u1:C1"), "Inbox review.html") == key


async def test_a_docx_is_uploaded_to_the_telegram_chat_it_was_asked_in(world):
    """Guards: `agent_worker_handler.py:224-225,238-239` → `ums.py:599-601` (override), `:606` (the port of the
    channel's own platform); Telegram posts no placeholder (`ums.py:615`)."""
    world.agent("doc_planner_agent").play(docx("Tom's plan.docx", "Tom's plan"))

    await world.run(body("create_document", "doc_planner_agent", TELEGRAM))

    require(not world.unscripted(), "every agent call was scripted")
    [upload] = world.media["telegram"].uploads
    assert (upload.channel_id, upload.filename, upload.title) == ("7001", "Tom's plan.docx", "Tom's plan")
    assert world.media["slack"].uploads == []
    assert world.channels.posts == []


async def test_a_docx_is_uploaded_to_the_slack_channel_it_was_asked_in(world):
    """Guards: `agent_worker_handler.py:224-225,238-239` → `ums.py:599-601,606,615-639` (the "📎" placeholder on the
    origin channel, then the upload to the channel it resolved)."""
    world.agent("doc_planner_agent").play(docx("Tom's plan.docx", "Tom's plan"))

    await world.run(body("create_document", "doc_planner_agent", OTHER_SLACK))

    require(not world.unscripted(), "every agent call was scripted")
    assert world.channels.posts == [Post("slack", "C2", "message", "📎")]
    [upload] = world.media["slack"].uploads
    assert (upload.channel_id, upload.filename) == ("C2", "Tom's plan.docx")
    assert world.media["telegram"].uploads == []


FAILURES = [
    pytest.param("create_html_page", "html_page_generator_agent",
                 "Document creation did not complete — generator down.", id="page"),
    pytest.param("create_document", "doc_planner_agent",
                 "Document creation did not complete — generator down.", id="docx"),
    pytest.param("tell_alek", "alek_agent",
                 "An errand the user gave during a phone call with Lelik did not complete. "
                 "Tell them it failed and what it was: q", id="errand"),
]


@pytest.mark.parametrize("intent,agent_id,alert", FAILURES)
async def test_a_failed_job_is_reported_in_the_chat_it_came_from(world, intent, agent_id, alert):
    """Guards: `agent_worker_handler.py:342-343` (`_notify_docx_failure`), `:360-361` (`_notify_errand_failure`) →
    `ums.py:350-352` (override); session `ums.py:376`."""
    world.agent(agent_id).play(fails())
    world.agent("quick_response_agent").play(says("That one did not work out."))

    await world.run(body(intent, agent_id, OTHER_SLACK))

    require(not world.unscripted(), "every agent call was scripted")
    assert [(p.kind, p.value) for p in world.posts_to(*OTHER_SLACK)] == [("text", "That one did not work out.")]
    assert world.posts_to(*PRIMARY) == []
    assert world.history("u1:C2") == [("user", alert, None),
                                      ("model", "That one did not work out.", "That one did not work out.")]
    assert world.history("u1:C1") == []


def research_event(event_type: str = "response.completed") -> dict:
    return {"task_type": "deep_research_webhook_event", "event_type": event_type, "job_id": "resp_1",
            "user_id": USER, "account_id": ACCOUNT, "session_id": "u1:7001", "origin_platform": "telegram",
            "query": QUERY}


def fetched_report():
    async def fetch(job_id: str) -> dict:
        return {"output_text": "Findings on heat pumps in Springfield.", "model": "m"}
    return fetch


async def test_a_finished_openai_research_report_reaches_the_chat_it_came_from(monkeypatch):
    """Guards: `worker_handler.py:668,671-674` (session, origin, dedup id) → `deep_research_delivery.py:294,299-300,
    302,303` (context and deadline of the report task) → `tds:33-40` → `gcp:207-231` (name, deadline) →
    `agent_worker_handler.py:258-259,281` (the page reaches the origin)."""
    world = SeamWorld(monkeypatch, deep_research_fetch=fetched_report())
    await world.set_primary(*PRIMARY)

    answer = await world.run(research_event())

    require(not world.unscripted(), "every agent call was scripted")
    assert answer == ({"status": "ok"}, 200)
    [request] = world.requests("agent_execution")
    assert request.name == "agent_execution-" + hashlib.sha256(b"deep_research_report:resp_1").hexdigest()[:32]
    assert request.deadline_s == 720
    context = request.body["context"]
    assert (context["session_id"], context["origin_channel_id"], context["origin_platform"]) == (
        "u1:7001", "7001", "telegram")
    assert context[DEEP_RESEARCH_REPORT_CONTEXT_KEY] == marker_query(QUERY)

    world.agent("html_page_generator_agent").play(page("heat-pumps.html", "Heat pumps in Springfield.html"))
    await world.run(request.body)

    require(not world.unscripted(), "every agent call was scripted")
    assert [label for label, _ in world.docs_to(*TELEGRAM)] == ["Heat pumps in Springfield.html"]
    assert world.posts_to(*PRIMARY) == []
    [key] = [k for k in world.stored_keys() if k.startswith("docs/u1/")]
    assert document_note(world.history("u1:7001"), "Heat pumps in Springfield.html") == key


async def test_a_report_task_that_cannot_be_created_is_reported_in_the_chat_it_came_from(monkeypatch):
    """Guards: `worker_handler.py:671-673` (origin) → `deep_research_delivery.py:319-329` (the failure notice to the
    origin, `failure_notification`) → `ums.py:350-352`."""
    async def no_wait(delay):
        return None

    monkeypatch.setattr(deep_research_delivery, "asyncio", SimpleNamespace(sleep=no_wait))
    world = SeamWorld(monkeypatch, deep_research_fetch=fetched_report())
    await world.set_primary(*PRIMARY)
    world.client.fail_next(times=3)
    quick = world.agent("quick_response_agent").play(says("The heat pump report could not be prepared."))

    answer = await world.run(research_event())

    require(not world.unscripted(), "every agent call was scripted")
    assert answer == ({"status": "report_not_enqueued"}, 200)
    assert world.client.failed_attempts == 3
    assert world.requests("agent_execution") == []
    assert [p.value for p in world.posts_to(*TELEGRAM)] == ["The heat pump report could not be prepared."]
    assert world.posts_to(*PRIMARY) == []
    [message] = quick.received
    assert message.context["current_message_parts"][0].text.startswith(
        f"[System: {report_not_prepared_notice(QUERY)}")
