"""EmailSearchAgent `open_email_attachment`: routing to FileConversionService.open_email, the answers it turns into
responses, and the `email_file_context` history entries (EXTERNAL_FILES_RFC 4.7, plan U4 sections 2 A4 and 6)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.email_search_agent import EmailSearchAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.document_open import EmailOpening, OpenedFile
from src.domain.document_read import ReadOutcome, ReadStatus
from src.domain.email_details import EmailDetails
from src.domain.email_open_texts import EMAIL_OPEN_NOT_CONFIGURED_NOTE, EMAIL_GONE_NOTE
from src.domain.email_ref import EMAIL_FILE_CONTEXT_KEY, email_file_label, format_email_ref
from src.domain.native_format import NATIVE_FORMATS_KEY
from src.domain.user import PerformanceTier
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.services.email_search_service import EmailSearchService

MSG = "msg_example001"
REF = format_email_ref(MSG, "2")
LABEL = email_file_label("invoice_INV-2024-417.pdf", 2048, "application/pdf", REF)


def _agent(file_conversion):
    ctx = AgentExecutionContext(agent_type="email_search", provider=MagicMock(spec=LLMPort), model_name="m",
                                tier=PerformanceTier.ECO, capabilities=ProviderCapabilities(),
                                resilience_port=InMemoryProviderResilience())
    return EmailSearchAgent(
        config=AgentConfig(agent_id="email_search_agent_u1", agent_type="email_search", timeout_ms=300_000),
        execution_context=ctx, prompt_builder=MagicMock(), email_search_service=AsyncMock(spec=EmailSearchService),
        user_id="u1", file_conversion=file_conversion)


def _msg(**payload):
    return AgentMessage(intent=AgentIntent.QUERY, payload={"intent": "open_email_attachment", **payload},
                        sender="smart", recipient="email_search_agent", task_id="t",
                        context={"user_id": "u1", "account_id": "a1"})


def _service(opening):
    svc = AsyncMock()
    svc.open_email.return_value = opening
    return svc


def _opened_text():
    return EmailOpening(
        opened=OpenedFile(ReadOutcome(status=ReadStatus.TEXT, text="Total due 120"),
                          body="[File: invoice_INV-2024-417.pdf]\nTotal due 120\n[/File: invoice_INV-2024-417.pdf]"),
        ref=REF, name="invoice_INV-2024-417.pdf", size=2048, mime="application/pdf")


async def test_can_handle_the_new_intent():
    assert await _agent(None).can_handle(_msg(email_id=MSG, name="x.pdf")) is True


async def test_email_ref_is_parsed_into_message_and_part():
    svc = _service(_opened_text())
    response = await _agent(svc).execute(_msg(file_ref=REF))
    kw = svc.open_email.await_args.kwargs
    assert (kw["message_id"], kw["part_id"], kw["name"]) == (MSG, "2", None)
    assert response.status == AgentStatus.SUCCESS


async def test_email_id_and_name_route_by_name_and_filename_is_the_fallback():
    svc = _service(_opened_text())
    await _agent(svc).execute(_msg(email_id=MSG, filename="a.pdf"))
    kw = svc.open_email.await_args.kwargs
    assert (kw["message_id"], kw["part_id"], kw["name"]) == (MSG, None, "a.pdf")


async def test_bare_file_ref_with_email_id_is_the_name():
    svc = _service(_opened_text())
    await _agent(svc).execute(_msg(file_ref="a.pdf", email_id=MSG))
    kw = svc.open_email.await_args.kwargs
    assert (kw["message_id"], kw["part_id"], kw["name"]) == (MSG, None, "a.pdf")


async def test_email_id_alone_is_passed_without_name():
    svc = _service(_opened_text())
    await _agent(svc).execute(_msg(email_id=MSG))
    assert svc.open_email.await_args.kwargs["name"] is None


async def test_bad_email_ref_and_missing_ids_fail_with_the_expected_input():
    agent = _agent(_service(_opened_text()))
    assert (await agent.execute(_msg(file_ref="email:bad ref"))).status == AgentStatus.FAILED
    assert (await agent.execute(_msg())).status == AgentStatus.FAILED
    assert (await agent.execute(_msg(file_ref="a.pdf"))).status == AgentStatus.FAILED


async def test_opened_text_is_marked_and_carries_label_and_history_entry():
    response = await _agent(_service(_opened_text())).execute(_msg(file_ref=REF))
    assert "Total due 120" in response.result and "external_content" in response.result
    assert response.result.rstrip().endswith(LABEL)
    assert response.history_context == {EMAIL_FILE_CONTEXT_KEY: [{"label": LABEL, "ref": REF}]}


async def test_native_file_goes_as_file_data():
    opening = EmailOpening(opened=OpenedFile(None, native_path="/tmp/x.pdf", native_mime="application/pdf"),
                           ref=REF, name="invoice_INV-2024-417.pdf", size=2048, mime="application/pdf")
    response = await _agent(_service(opening)).execute(_msg(file_ref=REF))
    assert response.metadata["file_data"] == {"path": "/tmp/x.pdf", "mime_type": "application/pdf"}
    assert LABEL in response.result


async def test_choice_is_a_success_with_note_and_candidate_labels():
    other = format_email_ref(MSG, "3")
    other_label = email_file_label("invoice_INV-2024-417.pdf", 4096, "application/pdf", other)
    opening = EmailOpening(candidates=((LABEL, REF), (other_label, other)), note="[System: pass one ref]")
    response = await _agent(_service(opening)).execute(_msg(email_id=MSG, name="invoice_INV-2024-417.pdf"))
    assert response.status == AgentStatus.SUCCESS
    assert "pass one ref" in response.result and LABEL in response.result and other_label in response.result
    assert response.history_context[EMAIL_FILE_CONTEXT_KEY] == [
        {"label": LABEL, "ref": REF}, {"label": other_label, "ref": other}]


async def test_note_is_a_success():
    response = await _agent(_service(EmailOpening(note=EMAIL_GONE_NOTE))).execute(_msg(file_ref=REF))
    assert response.status == AgentStatus.SUCCESS and response.result == EMAIL_GONE_NOTE


async def test_no_file_service_says_not_configured():
    response = await _agent(None).execute(_msg(file_ref=REF))
    assert response.status == AgentStatus.SUCCESS and response.result == EMAIL_OPEN_NOT_CONFIGURED_NOTE


async def test_native_declaration_and_deadline_reach_the_open():
    svc = _service(_opened_text())
    message = _msg(file_ref=REF)
    message.context[NATIVE_FORMATS_KEY] = [{"mime": "application/pdf", "max_bytes": 1000, "max_pages": 5}]
    await _agent(svc).execute(message)
    kw = svc.open_email.await_args.kwargs
    assert kw["consumer"][0].mime == "application/pdf"
    import time
    assert 200 < kw["deadline_at"] - time.time() <= 270


async def test_unexpected_exception_is_a_failure():
    svc = AsyncMock()
    svc.open_email.side_effect = RuntimeError("boom")
    response = await _agent(svc).execute(_msg(file_ref=REF))
    assert response.status == AgentStatus.FAILED and "open_email_attachment failed" in response.error


async def test_get_details_records_the_attachments_as_history():
    agent = _agent(None)
    agent._search_service.get_details_with_parts.return_value = EmailDetails("body", ((LABEL, REF),))
    response = await agent.execute(AgentMessage(
        intent=AgentIntent.QUERY, payload={"intent": "get_email_details", "email_id": MSG},
        sender="s", recipient="r", task_id="t", context={"user_id": "u1"}))
    assert response.result == "body"
    assert response.history_context == {EMAIL_FILE_CONTEXT_KEY: [{"label": LABEL, "ref": REF}]}
