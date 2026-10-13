"""Partial model usage on an outage (adversarial reviews C-M1 / B-M3; USER_DRIVE_INDEXING_RFC §24.7-§24.9): what
the reader and triage spent before DriveModelUnavailable travels on the exception — pages (or images) sent, calls
and cost — and the agents count calls and cost from their ledger on failure paths and parse retries."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.drive_document_reader_agent import AGENT_TYPE as READER_TYPE, DriveDocumentReaderAgent
from src.agents.drive_image_triage_agent import AGENT_TYPE as TRIAGE_TYPE, DriveImageTriageAgent, TriageInput
from src.composition.drive_reader_runner import DriveReaderRunner
from src.composition.drive_triage_runner import DriveTriageRunner
from src.domain.agent import AgentConfig
from src.domain.drive_enrichment_calls import ModelReplyStatus
from src.domain.drive_extraction import DriveModelUnavailable, ExtractionRequest, TriageImage
from src.domain.exceptions import LLMClientError, LLMRateLimitError
from src.domain.user import UserBotConfig
from src.infrastructure.drive_model_call import DriveModelCallFailed
from tests.unit.agents.drive_model_fakes import RoutedLLM, ScriptedLLM, context, file_bytes, prompt_builder, reply

# claude-haiku-5-5 at $0.10 / $0.50 per million tokens; reply() bills 1000 in + 200 out
CALL_COST = 1000 * 0.10e-6 + 200 * 0.50e-6
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 20


def test_the_exception_carries_usage_and_defaults_to_none():
    exc = DriveModelUnavailable("429", pages_sent=80, images_sent=0, model_calls=9, model_cost=0.0024)
    assert (exc.pages_sent, exc.images_sent, exc.model_calls, exc.model_cost) == (80, 0, 9, 0.0024)
    assert str(exc) == "429"
    bare = DriveModelUnavailable("x")
    assert (bare.pages_sent, bare.images_sent, bare.model_calls, bare.model_cost) == (0, 0, 0, 0.0)


# =============================================================================
# agents: calls and cost from the ledger, also when the call fails
# =============================================================================

def _reader(script):
    llm = ScriptedLLM(script)
    return DriveDocumentReaderAgent(AgentConfig(agent_id="r", agent_type=READER_TYPE), context(llm),
                                    prompt_builder()), llm


async def _read(agent):
    return await agent.read(data=b"%PDF", mime_type="application/pdf", file_name="w.pdf", first_window=False,
                            account_id="acc")


async def test_reader_parse_retry_counts_two_calls_and_both_costs():
    agent, _ = _reader([reply(text="not json"), reply({"text": "x", "brief": None})])
    out = await _read(agent)
    assert out.calls == 2 and out.cost == pytest.approx(2 * CALL_COST)


async def test_reader_outage_after_a_parse_retry_carries_the_spent_call():
    agent, _ = _reader([reply(text="not json"), LLMRateLimitError("429", http_status=429)])
    with pytest.raises(DriveModelUnavailable) as failed:
        await _read(agent)
    assert failed.value.model_calls == 2 and failed.value.model_cost == pytest.approx(CALL_COST)


async def test_reader_invalid_output_carries_both_calls():
    agent, _ = _reader([reply(text="nope"), reply(text="nope")])
    with pytest.raises(DriveModelCallFailed) as failed:
        await _read(agent)
    assert failed.value.model_calls == 2 and failed.value.model_cost == pytest.approx(2 * CALL_COST)


def _triage_agent(script):
    llm = ScriptedLLM(script)
    return DriveImageTriageAgent(AgentConfig(agent_id="t", agent_type=TRIAGE_TYPE), context(llm),
                                 prompt_builder()), llm


async def test_triage_correction_turn_counts_two_calls():
    agent, _ = _triage_agent([reply({"images": []}),
                              reply({"images": [{"index": 1, "verdict": "photo", "description": "a cat",
                                                 "objects": [], "scene": "", "visible_text": ""}]})])
    out = await agent.classify([TriageInput(1, JPEG, "image/jpeg", False)], account_id="acc")
    assert out.calls == 2 and out.usd == pytest.approx(2 * CALL_COST)   # triage v2 reports dollars as `usd`


async def test_triage_outage_after_a_correction_turn_carries_the_spent_call():
    agent, _ = _triage_agent([reply({"images": []}), LLMRateLimitError("429", http_status=429)])
    with pytest.raises(DriveModelUnavailable) as failed:
        await agent.classify([TriageInput(1, JPEG, "image/jpeg", False)], account_id="acc")
    assert failed.value.model_calls == 2 and failed.value.model_cost == pytest.approx(CALL_COST)


# =============================================================================
# reader runner: a 120-page file whose 9th window hits an outage
# =============================================================================

class _Cutter:
    def __init__(self, pages_total):
        self.pages_total = pages_total

    async def pdf_page_count(self, path):
        return self.pages_total

    async def cut_pdf_window(self, path, first_page, page_count, out_path):
        with open(out_path, "wb") as fh:
            fh.write(f"w{first_page // 10 + 1}".encode())


def _users():
    users = AsyncMock()
    users.get_user.return_value = SimpleNamespace(config=UserBotConfig())
    return users


def _contexts(llm):
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    return contexts


async def test_reader_outage_on_window_9_carries_windows_1_to_8(tmp_path):
    async def handler(request):
        window = int(file_bytes(request).decode()[1:])
        if window <= 8:
            return reply({"text": f"window {window}", "brief": None})
        if window == 9:
            await asyncio.sleep(0.05)
            return LLMRateLimitError("429", http_status=429)
        await asyncio.sleep(5)   # in flight when window 9 fails: cancelled, never answered
        return reply({"text": "late", "brief": None})

    source = tmp_path / "f.pdf"
    source.write_bytes(b"%PDF")
    llm = RoutedLLM(handler)
    runner = DriveReaderRunner(_contexts(llm), _users(), prompt_builder(), _Cutter(120))
    request = ExtractionRequest(path=str(source), name="f.pdf", kind="pdf", mime_type="application/pdf",
                                user_id="u", account_id="a", max_model_pages=500)
    with pytest.raises(DriveModelUnavailable) as failed:
        await asyncio.wait_for(runner.extract(request), timeout=3)
    assert failed.value.pages_sent == 80
    assert failed.value.model_cost == pytest.approx(8 * CALL_COST)
    assert failed.value.model_calls == 9   # eight answered, the ninth refused with a 429


# =============================================================================
# triage runner: an outage after part of the batch was answered
# =============================================================================

async def test_triage_outage_after_a_halved_batch_carries_the_answered_half():
    def handler(request):
        n = sum(1 for p in request.messages[0].parts if p.file_data)
        if n == 4:
            return LLMClientError("bad image", http_status=400)
        first = request.messages[0].parts[0].text
        if first.startswith("Image 1:"):
            return reply({"images": [{"index": i, "verdict": "photo", "description": "x", "objects": [], "scene": "",
                                      "visible_text": ""} for i in (1, 2)]})
        return LLMRateLimitError("429", http_status=429)

    llm = RoutedLLM(handler)
    runner = DriveTriageRunner(_contexts(llm), _users(), prompt_builder())
    images = [TriageImage(item_id=f"i{n}", thumbnail=JPEG, mime_type="image/jpeg", camera_prior=False)
              for n in range(4)]
    # triage v2 (§25.13.1): the outage is the reply's status, never an exception, and the answered half travels as
    # results with the calls made and their cost
    out = await runner.triage("u", "a", images)
    assert out.status is ModelReplyStatus.UNAVAILABLE
    assert ([r.item_id for r in out.results], out.model_calls) == (["i0", "i1"], 3)
    assert out.usd == pytest.approx(CALL_COST)
