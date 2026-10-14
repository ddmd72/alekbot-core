"""Search v2 P3 review fixes in the runners (USER_DRIVE_INDEXING_RFC §25.3, §25.4.4):
- M1: a scan (an image triaged as text, read by the reader) gets the full profile, key values included;
- M2: the fallback's safety block reaches the port as `failed`, never `refused`;
- m2: after a capped correction turn, triage asks again only for the images still unanswered."""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.composition.drive_profiler_runner import DriveProfilerRunner
from src.composition.drive_triage_runner import DriveTriageRunner
from src.domain.drive_enrichment_calls import ModelReplyStatus as S, ProfileInput, ProfileRequest
from src.domain.drive_extraction import TriageImage
from src.domain.exceptions import LLMServerError
from src.domain.llm import FinishReason, ProviderCapabilities
from src.domain.user import PerformanceTier, UserBotConfig
from src.ports.llm_port import AgentExecutionContext
from tests.unit.agents.drive_model_fakes import RoutedLLM, ScriptedLLM, prompt_builder, reply
from tests.unit.agents.test_drive_profiler_agent import ANSWER


def _ctx(llm, fallback=None, agent_type="drive_profiler"):
    return AgentExecutionContext(
        agent_type=agent_type, provider=llm, model_name="gpt-6-luna", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(vision=True), provider_name="openai", fallback_provider=fallback,
        fallback_model_name="gemini-3.8-flash" if fallback else None,
        fallback_provider_name="gemini" if fallback else None, resilience_port=InMemoryProviderResilience())


def _contexts(ctx):
    contexts = MagicMock()
    contexts.build.return_value = ctx
    users = AsyncMock()
    users.get_user.return_value = SimpleNamespace(config=UserBotConfig())
    return contexts, users


async def test_a_scan_is_asked_for_key_values():
    llm = ScriptedLLM([reply(ANSWER)])
    contexts, users = _contexts(_ctx(llm))
    out = await DriveProfilerRunner(contexts, users, prompt_builder()).profile(ProfileRequest(
        user_id="u", account_id="a", item_id="i", kind="image", name="IMG_1.jpg",
        input=ProfileInput(head="Ticket 4411 " * 30)))
    assert "has no key values" not in llm.requests[0].messages[0].parts[0].text
    assert out.profile.key_values != ()


async def test_a_fallback_safety_block_is_failed_at_the_port():
    contexts, users = _contexts(_ctx(ScriptedLLM([LLMServerError("500", http_status=500)]),
                                     ScriptedLLM([reply(text="", finish=FinishReason.SAFETY)])))
    out = await DriveProfilerRunner(contexts, users, prompt_builder()).profile(ProfileRequest(
        user_id="u", account_id="a", item_id="i", kind="pdf", name="f.pdf", input=ProfileInput(head="x " * 200)))
    assert out.status is S.FAILED


async def test_triage_asks_again_only_for_the_unanswered_images():
    def labels(request):
        return [int(n) for n in re.findall(r"^Image (\d+):", "\n".join(
            p.text for p in request.messages[0].parts if p.text), re.M)]

    def handler(request):
        if len(request.messages) > 1:
            return reply(text='{"images": [', finish=FinishReason.MAX_TOKENS)
        return reply({"images": [{"index": labels(request)[0], "verdict": "photo", "description": "a hill",
                                  "objects": [], "scene": "", "visible_text": ""}]})

    llm = RoutedLLM(handler)
    contexts, users = _contexts(_ctx(llm, agent_type="drive_triage"))
    images = [TriageImage(item_id=f"item-{n}", thumbnail=b"\xff\xd8\xff\xe0" + bytes([n]), mime_type="",
                          camera_prior=False) for n in (1, 2)]
    out = await DriveTriageRunner(contexts, users, prompt_builder()).triage("u", "a", images)
    assert {r.item_id for r in out.results} == {"item-1", "item-2"}
    assert [labels(r) for r in llm.requests if len(r.messages) == 1] == [[1, 2], [2]]
