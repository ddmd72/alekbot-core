"""P18: the specialist's file_ref rides the Cloud Task as a ref and is opened by the worker just before it runs
(EXTERNAL_FILES_RFC §4.2), on the seam world; plus the coordinator's SYNC prefetch and its containment."""
import logging
from types import SimpleNamespace
from typing import List, Tuple

import pytest

from src.domain.document_open import Prefetched
from src.domain.document_open_texts import conversion_alert
from src.domain.document_read import INTERACTIVE_OPEN_S, PREFETCH_OPEN_S
from src.domain.request_context import get_current_user_id
from src.handlers import agent_worker_handler
from src.infrastructure import agent_coordinator
from tests.support.seam_world import ACCOUNT, USER, Delegation, SeamWorld, Step, fails, require, says

REF = "invoice_painting_es.pdf"
QUERY = "a one-page summary of the painting invoice for 12 Oak Street"
TEXT = "[File: invoice_painting_es.pdf]\nInvoice 2026/078: painting, 12 Oak Street"
NOW = 1_000_000.0


class Resolver:
    """The prefetch seam: records (ref, user, account, deadline, tenant seen) and answers a scripted `Prefetched`."""

    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer or Prefetched(content=TEXT), error
        self.calls: List[Tuple[str, str, str, float, object]] = []

    async def __call__(self, ref, user_id, account_id, deadline_at):
        self.calls.append((ref, user_id, account_id, deadline_at, get_current_user_id()))
        if self.error:
            raise self.error
        return self.answer


@pytest.fixture
async def world(monkeypatch):
    w = SeamWorld(monkeypatch)
    clock = SimpleNamespace(time=lambda: NOW)
    monkeypatch.setattr(agent_worker_handler, "time", clock)
    monkeypatch.setattr(agent_coordinator, "time", clock)
    return w


def _task(world, intent, agent_id, **params):
    return {"task_type": "agent_execution", "agent_id": agent_id, "intent": intent, "query": QUERY,
            "context": {"user_id": USER, "account_id": ACCOUNT, "session_id": f"{USER}:C1", "origin_channel_id": "C1",
                        "origin_platform": "slack", "params": params}}


async def test_async_delegation_enqueues_the_ref_and_no_file_text(world):
    resolver = Resolver()
    world.coordinator._file_ref_resolver = resolver
    smart = world.agent("smart_response_agent").play(
        Step(says("On it."), Delegation("create_pdf", QUERY, {"file_ref": REF})))
    await smart.process(_smart_message())
    body = world.bodies("agent_execution")[0]
    assert body["context"]["params"] == {"file_ref": REF}
    assert "file_content" not in str(body)
    assert resolver.calls == []  # nothing is opened before the task is enqueued


def _smart_message():
    from src.domain.agent import AgentIntent, AgentMessage
    return AgentMessage(intent=AgentIntent.QUERY, payload={"query": QUERY}, sender="user", recipient=f"smart_{USER}",
                        task_id="s1", context={"user_id": USER, "account_id": ACCOUNT, "session_id": f"{USER}:C1",
                                               "origin_channel_id": "C1", "origin_platform": "slack"})


async def test_worker_opens_the_ref_before_the_specialist_with_the_prefetch_deadline(world):
    resolver = Resolver()
    world.coordinator._file_ref_resolver = resolver
    pdf = world.agent("pdf_generator_agent").always(says("done"))
    await world.run(_task(world, "create_pdf", "pdf_generator_agent", file_ref=REF))
    require(not world.unscripted(), "the stand-in ran out of steps")
    assert resolver.calls == [(REF, USER, ACCOUNT, NOW + PREFETCH_OPEN_S, USER)]  # inside the tenant context
    assert PREFETCH_OPEN_S == 270.0
    assert pdf.received[0].payload["file_content"] == TEXT
    assert "file_note" not in pdf.received[0].payload


async def test_an_opted_out_agent_gets_nothing(world):
    resolver = Resolver()
    world.coordinator._file_ref_resolver = resolver
    files = world.agent("file_management_agent").always(says("done"))
    descriptor = world.registry.get_descriptor("file_management_agent")
    require(descriptor is not None and not descriptor.prefetch_file_ref, "the agent opts out of the prefetch")
    intent = next(iter(descriptor.capabilities))
    await world.run(_task(world, intent, "file_management_agent", file_ref=REF))
    require(not world.unscripted(), "the stand-in ran out of steps")
    assert resolver.calls == []
    assert "file_content" not in files.received[0].payload and "file_note" not in files.received[0].payload


async def test_a_failed_open_gives_a_note_and_no_content(world, caplog):
    note = conversion_alert(REF, "application/pdf")
    world.coordinator._file_ref_resolver = Resolver(Prefetched(note=note))
    pdf = world.agent("pdf_generator_agent").always(says("done"))
    with caplog.at_level(logging.WARNING):
        await world.run(_task(world, "create_pdf", "pdf_generator_agent", file_ref=REF))
    payload = pdf.received[0].payload
    assert payload["file_note"] == note and "file_content" not in payload
    assert any(r.levelno == logging.WARNING and REF in r.getMessage() for r in caplog.records)


async def test_a_resolver_that_raises_is_contained_with_a_note_and_an_error_line(world, caplog):
    world.coordinator._file_ref_resolver = Resolver(error=RuntimeError("boom"))
    pdf = world.agent("pdf_generator_agent").always(says("done"))
    with caplog.at_level(logging.WARNING):
        answer = await world.run(_task(world, "create_pdf", "pdf_generator_agent", file_ref=REF))
    require(not world.unscripted(), "the stand-in ran out of steps")
    assert answer is not None
    payload = pdf.received[0].payload
    assert payload["file_note"] == conversion_alert(REF) and "file_content" not in payload  # honest, no raw exception
    assert "boom" not in payload["file_note"]
    assert any(r.levelno == logging.ERROR and REF in r.getMessage() for r in caplog.records)


async def test_a_failed_generation_after_a_note_tells_the_user(world):
    """The real failure path: the specialist fails with the note, the worker's failure notice reaches the origin."""
    note = conversion_alert(REF, "application/pdf")
    world.coordinator._file_ref_resolver = Resolver(Prefetched(note=note))
    world.agent("pdf_generator_agent").always(fails(note))
    world.agent("quick_response_agent").play(says("I could not read that invoice."))
    await world.run(_task(world, "create_pdf", "pdf_generator_agent", file_ref=REF))
    require(not world.unscripted(), "the stand-in ran out of steps")
    assert [p.value for p in world.posts_to("slack", "C1")] == ["I could not read that invoice."]  # not silence
    assert note in world.history(f"{USER}:C1")[0][1]  # the note is what the notice was written from


async def test_sync_delegation_still_prefetches_in_the_caller_with_the_interactive_deadline(world):
    resolver = Resolver()
    world.coordinator._file_ref_resolver = resolver
    notes = world.agent("notes_agent").always(says("ok"))
    descriptor = world.registry.get_descriptor("notes_agent")
    intent = next(i for i, mode in descriptor.capabilities.items() if mode.value == "sync")
    await world.coordinator.handle_delegation(
        intent, QUERY, {"user_id": USER, "account_id": ACCOUNT, "params": {"file_ref": "gym_pass_uk.md"}}, "smart")
    require(not world.unscripted(), "the stand-in ran out of steps")
    assert resolver.calls == [("gym_pass_uk.md", USER, ACCOUNT, NOW + INTERACTIVE_OPEN_S, None)]
    assert notes.received[0].payload["file_content"] == TEXT
