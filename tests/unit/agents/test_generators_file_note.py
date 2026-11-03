"""P18: a PDF or HTML generator whose file could not be read fails with the prefetch note and never renders it."""
import logging
from unittest.mock import AsyncMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.html_page_generator_agent import HtmlPageGeneratorAgent
from src.agents.pdf_generator_agent import PdfGeneratorAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.document_open_texts import conversion_alert
from src.domain.llm import LLMResponse
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.puppeteer_runner_port import PuppeteerRunnerPort

QUERY = "a one-page summary of the painting invoice for 12 Oak Street"
NOTE = conversion_alert("invoice_painting_es.pdf", "application/pdf")


def _context(agent_type: str, llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type=agent_type, provider=llm, model_name="test-model", tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )


def _build(kind: str):
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = LLMResponse(text="<html><head><title>T</title></head><body>x</body></html>",
                                                    tool_calls=[])
    prompts = AsyncMock(spec=PromptBuilderPort)
    prompts.build_for_agent.return_value = "system prompt"
    if kind == "pdf":
        runner = AsyncMock(spec=PuppeteerRunnerPort)
        runner.run.return_value = b"%PDF-1.4"
        agent = PdfGeneratorAgent(config=AgentConfig(agent_id="pdf_generator_agent_ann", agent_type="pdf_generator"),
                                  execution_context=_context("pdf_generator", llm), pdf_runner=runner,
                                  prompt_builder=prompts, user_id="ann")
    else:
        agent = HtmlPageGeneratorAgent(
            config=AgentConfig(agent_id="html_page_generator_agent_ann", agent_type="html_page"),
            execution_context=_context("html_page", llm), prompt_builder=prompts, user_id="ann")
    return agent, llm


def _message(**payload) -> AgentMessage:
    return AgentMessage(intent=AgentIntent.DELEGATE, payload={"query": QUERY, **payload}, sender="smart",
                        recipient="generator", task_id="t1", context={"user_id": "ann", "account_id": "acc-ann"})


@pytest.mark.parametrize("kind", ["pdf", "html"])
async def test_a_file_note_fails_the_task_with_the_note_and_renders_nothing(kind, caplog):
    agent, llm = _build(kind)
    with caplog.at_level(logging.WARNING):
        response = await agent.execute(_message(file_ref="invoice_painting_es.pdf", file_note=NOTE))
    assert response.status == AgentStatus.FAILED
    assert response.error == NOTE
    llm.generate_content.assert_not_called()  # the note never reaches a prompt, so never a document
    assert any(r.levelno >= logging.WARNING and "not readable" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("kind", ["pdf", "html"])
async def test_content_wins_over_a_note_and_generation_runs(kind):
    agent, llm = _build(kind)
    response = await agent.execute(_message(file_content="Invoice 2026/078: painting, 12 Oak Street", file_note=NOTE))
    assert response.status == AgentStatus.SUCCESS
    llm.generate_content.assert_called()
