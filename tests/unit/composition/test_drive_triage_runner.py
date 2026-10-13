"""DriveTriageRunner (USER_DRIVE_INDEXING_RFC §5.3, §24.5, §24.8; triage v2 §25.4.3, §25.4.6, §25.13.1): one result per
image answered, by item id, with the batch's calls, model and cost; opaque indexes; a refused batch halved down to the
refused image, which is reported refused (no description); an unsendable rendition is reported refused and never
sent; an image left unanswered is unsettled (no result, retried); an outage is a status, never an exception; built from
the user's configuration and billed to the account. Holiday photos cost one call per TRIAGE_BATCH and no reader call
(§16 Slice 4 invariant, at this port)."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.drive_triage_runner import DriveTriageRunner
import math

from src.domain.drive_enrichment import PhotoDescription
from src.domain.drive_enrichment_calls import ModelReplyStatus, TriageResultV2
from src.domain.drive_extraction import TriageImage
from src.domain.drive_index import TriageVerdict
from src.domain.drive_index_params import TRIAGE_BATCH
from src.domain.exceptions import LLMClientError, LLMServerError
from src.domain.user import UserBotConfig
from tests.unit.agents.drive_model_fakes import RoutedLLM, context, prompt_builder, reply

TEXT, PHOTO = TriageVerdict.TEXT, TriageVerdict.PHOTO
JPEG = b"\xff\xd8\xff\xe0"


def _image(n, data=None, prior=False):
    return TriageImage(item_id=f"item-{n}", thumbnail=data if data is not None else JPEG + bytes([n]),
                       mime_type="image/heic", camera_prior=prior)


def _indexes(request):
    return [int(p.text.split()[1].rstrip(":")) for p in request.messages[0].parts
            if p.text and p.text.startswith("Image ")]


def _entry(i, verdict="photo", description=""):
    return {"index": i, "verdict": verdict, "description": description, "objects": [], "scene": "",
            "visible_text": ""}


def _all_photos(request):
    return reply({"images": [_entry(i, description=f"photo {i}") for i in _indexes(request)]})


def _photo(item_id, description):
    return TriageResultV2(item_id, PHOTO, PhotoDescription(description=description))


S = ModelReplyStatus


def _runner(handler, config=None, quota=None, builder=None, user_missing=False):
    llm = RoutedLLM(handler)
    users = AsyncMock()
    users.get_user.return_value = None if user_missing else SimpleNamespace(config=config or UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    return DriveTriageRunner(contexts, users, builder or prompt_builder(), quota_service=quota), llm, contexts


async def test_one_result_per_image_by_item_id():
    def handler(request):
        return reply({"images": [_entry(2, "text"), _entry(1, description="a red bicycle")]})

    runner, llm, _ = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(1, prior=True), _image(2)])
    assert out.results == (_photo("item-1", "a red bicycle"), TriageResultV2("item-2", TEXT, None))
    assert out.status is S.OK and (out.capped_item_ids, out.refused_item_ids) == ((), ())
    assert out.model_calls == 1 and out.usd == pytest.approx(1000 * 0.10e-6 + 200 * 0.50e-6)
    assert out.model == "claude-haiku-5-5" and out.fallback_usd == 0.0
    sent = " ".join(p.text or "" for p in llm.requests[0].messages[0].parts)
    assert "item-1" not in sent and "Image 1: camera metadata present" in sent
    assert llm.requests[0].messages[0].parts[1].file_data["mime_type"] == "image/jpeg"


async def test_holiday_photos_cost_one_call_per_batch():
    runner, llm, _ = _runner(_all_photos)
    """v2 batches of TRIAGE_BATCH (8, §25.4.3): ⌈N / TRIAGE_BATCH⌉ calls."""
    out = await runner.triage("user-1", "acc-1", [_image(n, prior=True) for n in range(1, 17)])
    calls = math.ceil(16 / TRIAGE_BATCH)
    assert len(llm.requests) == calls and out.model_calls == calls
    assert {r.verdict for r in out.results} == {PHOTO} and len(out.results) == 16
    assert [len(_indexes(r)) for r in llm.requests] == [TRIAGE_BATCH] * calls


async def test_empty_batch_makes_no_call():
    runner, llm, contexts = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [])
    assert (out.status, out.results, out.model_calls, out.usd) == (S.OK, (), 0, 0.0)
    assert llm.requests == [] and contexts.build.call_count == 0


async def test_unanswered_images_are_unsettled_and_retried():
    """v2 (§25.13.1): an image still unanswered after the correction turn has no result and is neither capped nor
    refused — the caller retries it; the batch is `invalid` (charged, no backoff)."""
    runner, llm, _ = _runner(lambda r: reply({"images": [_entry(1, "text")]}))
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2)])
    assert out.results == (TriageResultV2("item-1", TEXT, None),) and len(llm.requests) == 2   # the correction turn
    assert (out.capped_item_ids, out.refused_item_ids, out.status) == ((), (), S.INVALID)


async def test_unsendable_rendition_is_refused_and_not_sent():
    """v2 (§25.13.1): no provider takes it, so it is reported refused (no description), never sent."""
    runner, llm, _ = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2, data=b"<svg/>")])
    assert out.results == (_photo("item-1", "photo 1"),) and out.refused_item_ids == ("item-2",)
    assert _indexes(llm.requests[0]) == [1] and out.status is S.OK


async def test_only_unsendable_renditions_make_no_call():
    runner, llm, contexts = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [_image(1, data=b"")])
    assert (out.results, out.refused_item_ids, out.model_calls, out.status) == ((), ("item-1",), 0, S.OK)
    assert llm.requests == [] and contexts.build.call_count == 0


async def test_refused_batch_is_halved_down_to_the_refused_image():
    def handler(request):
        sent = [base64.b64decode(p.file_data["base64"]) for p in request.messages[0].parts if p.file_data]
        return LLMClientError("image rejected", http_status=400) if JPEG + bytes([3]) in sent else _all_photos(request)

    runner, llm, _ = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 5)])
    assert out.refused_item_ids == ("item-3",) and out.status is S.OK   # refused alone: no description
    assert out.results == (_photo("item-1", "photo 1"), _photo("item-2", "photo 2"), _photo("item-4", "photo 4"))
    assert out.model_calls == 5   # the batch, two halves, two quarters
    assert [_indexes(r) for r in llm.requests] == [[1, 2, 3, 4], [1, 2], [3, 4], [3], [4]]


async def test_a_server_error_is_a_failed_status_for_the_batch_not_an_exception():
    """v2 (§25.4.6, §25.13.1): a 5xx after a call is `failed` (charged, backoff); the port never raises."""
    runner, llm, _ = _runner(lambda r: LLMServerError("529", http_status=529))
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2)])
    assert (out.status, out.results, out.capped_item_ids, out.refused_item_ids) == (S.FAILED, (), (), ())
    assert out.model_calls == 1 and len(llm.requests) == 1


async def test_missing_prompt_is_an_uncharged_outage():
    runner, llm, _ = _runner(_all_photos, builder=prompt_builder(prompt=""))
    out = await runner.triage("user-1", "acc-1", [_image(1)])
    assert (out.status, out.results, out.model_calls, out.usd) == (S.UNAVAILABLE, (), 0, 0.0)
    assert llm.requests == []


async def test_built_from_the_users_config_and_billed_to_the_account():
    config = UserBotConfig(agent_providers={"drive_triage": "openai"})
    quota = AsyncMock()
    runner, _, contexts = _runner(_all_photos, config=config, quota=quota)
    await runner.triage("user-1", "acc-9", [_image(1)])
    contexts.build.assert_called_once_with("drive_triage", config)
    assert quota.record_usage.await_args.kwargs["account_id"] == "acc-9"


async def test_missing_user_or_provider_is_an_outage():
    runner, llm, _ = _runner(_all_photos, user_missing=True)
    out = await runner.triage("user-1", "acc-1", [_image(1)])
    assert (out.status, out.results, out.model_calls) == (S.UNAVAILABLE, (), 0) and llm.requests == []
    runner, llm, contexts = _runner(_all_photos)
    contexts.build.side_effect = ValueError("Provider 'openai' not registered")
    out = await runner.triage("user-1", "acc-1", [_image(1)])
    assert (out.status, out.results, out.model_calls) == (S.UNAVAILABLE, (), 0) and llm.requests == []

