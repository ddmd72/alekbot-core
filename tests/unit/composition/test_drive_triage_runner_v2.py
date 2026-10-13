"""DriveTriageRunner, triage v2 (USER_DRIVE_INDEXING_RFC §25.4.3, §25.4.4, §25.4.6, §25.13.1): the triage agent as the
v2 ImageTriagePort — one TriageResultV2 per image answered, by item id; batches of TRIAGE_BATCH; a batch stopped at
the cap or refused halved down to the image (`capped_item_ids`, `refused_item_ids`); unsendable thumbnails refused;
every outcome a status, never an exception; the model, cost and fallback share."""
import inspect
import re
import typing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.composition.drive_triage_runner import DriveTriageRunner
from src.domain.billing import calculate_cost
from src.domain.drive_enrichment import PhotoDescription
from src.domain.drive_enrichment_calls import ModelReplyStatus as S, TriageReply, TriageResultV2
from src.domain.drive_extraction import TriageImage
from src.domain.drive_index import TriageVerdict
from src.domain.drive_index_params import TRIAGE_BATCH
from src.domain.exceptions import LLMClientError, LLMRateLimitError, LLMServerError
from src.domain.llm import FinishReason, ProviderCapabilities
from src.domain.user import PerformanceTier, UserBotConfig
from src.ports.image_triage_port import ImageTriagePort
from src.ports.llm_port import AgentExecutionContext
from tests.unit.agents.drive_model_fakes import RoutedLLM, prompt_builder, reply

TEXT, PHOTO = TriageVerdict.TEXT, TriageVerdict.PHOTO
JPEG = b"\xff\xd8\xff\xe0"


def _image(n, data=None):
    return TriageImage(item_id=f"item-{n}", thumbnail=data if data is not None else JPEG + bytes([n]),
                       mime_type="image/heic", camera_prior=False)


def _indexes(request):
    return [int(p.text.split()[1].rstrip(":")) for p in request.messages[0].parts
            if p.text and p.text.startswith("Image ")]


def _ids(request):
    """The item number each thumbnail of the request stands for (its last byte)."""
    return [p.file_data and __import__("base64").b64decode(p.file_data["base64"])[-1]
            for p in request.messages[0].parts if p.file_data]


def _photo(i):
    return {"index": i, "verdict": "photo", "description": f"photo {i}", "objects": [], "scene": "", "visible_text": ""}


def _all_photos(request):
    return reply({"images": [_photo(i) for i in _indexes(request)]})


def _ctx(llm, fallback=None):
    return AgentExecutionContext(
        agent_type="drive_triage", provider=llm, model_name="gpt-6-luna", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(vision=True), provider_name="openai", fallback_provider=fallback,
        fallback_model_name="gemini-3.8-flash" if fallback else None,
        fallback_provider_name="gemini" if fallback else None, resilience_port=InMemoryProviderResilience())


def _runner(handler, fallback_handler=None, quota=None, user_missing=False):
    llm = RoutedLLM(handler)
    fallback = RoutedLLM(fallback_handler) if fallback_handler else None
    users = AsyncMock()
    users.get_user.return_value = None if user_missing else SimpleNamespace(config=UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = _ctx(llm, fallback)
    return DriveTriageRunner(contexts, users, prompt_builder(), quota_service=quota), llm


def test_the_port_returns_the_v2_reply():
    assert typing.get_type_hints(ImageTriagePort.triage)["return"] is TriageReply
    assert list(inspect.signature(ImageTriagePort.triage).parameters)[1:] == ["user_id", "account_id", "images"]


async def test_one_result_per_image_by_item_id():
    def handler(request):
        return reply({"images": [{"index": 2, "verdict": "text", "description": "", "objects": [], "scene": "",
                                  "visible_text": ""}, _photo(1)]})

    runner, llm = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2)])
    assert out.status is S.OK
    assert out.results == (TriageResultV2("item-1", PHOTO, PhotoDescription(description="photo 1")),
                           TriageResultV2("item-2", TEXT, None))
    assert out.model_calls == 1 and out.model == "gpt-6-luna"
    assert out.usd == pytest.approx(calculate_cost("gpt-6-luna", 1000, 200)) and out.fallback_usd == 0
    sent = " ".join(p.text or "" for p in llm.requests[0].messages[0].parts)
    assert "item-1" not in sent and llm.requests[0].messages[0].parts[1].file_data["mime_type"] == "image/jpeg"


async def test_batches_of_triage_batch():
    runner, llm = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 2 * TRIAGE_BATCH + 2)])
    assert [len(_indexes(r)) for r in llm.requests] == [TRIAGE_BATCH, TRIAGE_BATCH, 1]
    assert len(out.results) == 2 * TRIAGE_BATCH + 1 and out.model_calls == 3


async def test_billed_to_the_account():
    quota = AsyncMock()
    runner, _ = _runner(_all_photos, quota=quota)
    await runner.triage("user-1", "acc-1", [_image(1)])
    assert quota.record_usage.await_args.kwargs["account_id"] == "acc-1"


async def test_a_batch_stopped_at_the_cap_is_halved_down_to_the_image():
    def handler(request):
        if 3 in _ids(request):
            return reply(text='{"images": [', finish=FinishReason.MAX_TOKENS)
        return _all_photos(request)

    runner, llm = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 5)])
    assert out.capped_item_ids == ("item-3",) and {r.item_id for r in out.results} == {"item-1", "item-2", "item-4"}
    assert out.status is S.STOPPED_AT_CAP


async def test_a_refused_batch_is_halved_and_the_refused_image_listed():
    def handler(request):
        if 2 in _ids(request):
            return LLMClientError("refused", http_status=400)
        return _all_photos(request)

    runner, _ = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 5)])
    assert out.refused_item_ids == ("item-2",) and len(out.results) == 3
    assert out.status is S.OK


async def test_unsendable_thumbnails_are_refused_without_a_call():
    runner, llm = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [_image(1, data=b"not an image"), _image(2)])
    assert out.refused_item_ids == ("item-1",) and [r.item_id for r in out.results] == ["item-2"]
    assert len(_indexes(llm.requests[0])) == 1


async def test_an_outage_is_a_status_and_cuts_the_rest():
    runner, llm = _runner(lambda r: LLMRateLimitError("429", http_status=429))
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 2 * TRIAGE_BATCH)])
    assert isinstance(out, TriageReply) and out.status is S.UNAVAILABLE and out.results == ()
    assert len(llm.requests) == 1


async def test_a_failure_is_failed_and_cuts_the_rest():
    runner, llm = _runner(lambda r: LLMServerError("500", http_status=500))
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 2 * TRIAGE_BATCH)])
    assert out.status is S.FAILED and out.results == () and len(llm.requests) == 1


async def test_invalid_output_leaves_the_images_unsettled():
    runner, _ = _runner(lambda r: reply(text="nope"))
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2)])
    assert out.status is S.INVALID and out.results == () and out.capped_item_ids == out.refused_item_ids == ()


async def test_an_image_left_out_is_unsettled_and_invalid():
    runner, _ = _runner(lambda r: reply({"images": [_photo(1)]}))
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2)])
    assert [r.item_id for r in out.results] == ["item-1"] and out.status is S.INVALID


async def test_the_fallback_serves_with_its_share():
    runner, _ = _runner(lambda r: LLMServerError("500", http_status=500), fallback_handler=_all_photos)
    out = await runner.triage("user-1", "acc-1", [_image(1)])
    assert out.status is S.OK and out.model == "gemini-3.8-flash"
    assert out.fallback_usd == out.usd == pytest.approx(calculate_cost("gemini-3.8-flash", 1000, 200))


async def test_a_refusal_by_the_fallback_is_failed():
    runner, _ = _runner(lambda r: LLMServerError("500", http_status=500),
                        fallback_handler=lambda r: LLMClientError("no", http_status=400))
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2)])
    assert out.status is S.FAILED and out.refused_item_ids == ()


async def test_no_user_is_unavailable():
    runner, _ = _runner(_all_photos, user_missing=True)
    out = await runner.triage("user-1", "acc-1", [_image(1)])
    assert out.status is S.UNAVAILABLE


async def test_empty_batch():
    runner, llm = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [])
    assert out.status is S.OK and out.results == () and llm.requests == []
