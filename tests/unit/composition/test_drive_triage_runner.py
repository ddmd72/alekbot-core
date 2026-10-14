"""DriveTriageRunner (USER_DRIVE_INDEXING_RFC §5.3, §24.5, §24.8): one result per image given, by item id, with the batch's calls and cost; opaque
indexes; a refused batch halved down to the refused image; unanswered and unsendable images are photographs without a
caption; an outage raises; built from the user's configuration and billed to the account. Holiday photos cost one
call per batch and no reader call (§16 Slice 4 invariant, at this port)."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.drive_triage_runner import DriveTriageRunner
from src.domain.drive_extraction import DriveModelUnavailable, TriageImage, TriageResult
from src.domain.drive_index import TriageVerdict
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


def _all_photos(request):
    return reply({"images": [{"index": i, "verdict": "photo", "caption": f"photo {i}"} for i in _indexes(request)]})


def _runner(handler, config=None, quota=None, builder=None, user_missing=False):
    llm = RoutedLLM(handler)
    users = AsyncMock()
    users.get_user.return_value = None if user_missing else SimpleNamespace(config=config or UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    return DriveTriageRunner(contexts, users, builder or prompt_builder(), quota_service=quota), llm, contexts


async def test_one_result_per_image_by_item_id():
    def handler(request):
        return reply({"images": [{"index": 2, "verdict": "text", "caption": ""},
                                 {"index": 1, "verdict": "photo", "caption": "a red bicycle"}]})

    runner, llm, _ = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(1, prior=True), _image(2)])
    assert out.results == (TriageResult("item-1", PHOTO, "a red bicycle"), TriageResult("item-2", TEXT, ""))
    assert out.model_calls == 1 and out.model_cost == pytest.approx(1000 * 0.10e-6 + 200 * 0.50e-6)
    sent = " ".join(p.text or "" for p in llm.requests[0].messages[0].parts)
    assert "item-1" not in sent and "Image 1: camera metadata present" in sent
    assert llm.requests[0].messages[0].parts[1].file_data["mime_type"] == "image/jpeg"


async def test_holiday_photos_cost_one_call_per_batch():
    runner, llm, _ = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [_image(n, prior=True) for n in range(1, 17)])
    assert len(llm.requests) == 1 and {r.verdict for r in out.results} == {PHOTO} and out.model_calls == 1


async def test_empty_batch_makes_no_call():
    runner, llm, contexts = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [])
    assert (out.results, out.model_calls, out.model_cost) == ((), 0, 0.0)
    assert llm.requests == [] and contexts.build.call_count == 0


async def test_unanswered_images_are_photographs_without_a_caption():
    runner, llm, _ = _runner(lambda r: reply({"images": [{"index": 1, "verdict": "text", "caption": ""}]}))
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2)])
    assert out.results[1] == TriageResult("item-2", PHOTO, "") and len(llm.requests) == 2   # the correction turn


async def test_unsendable_rendition_is_a_photograph_and_not_sent():
    runner, llm, _ = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2, data=b"<svg/>")])
    assert out.results[1] == TriageResult("item-2", PHOTO, "") and _indexes(llm.requests[0]) == [1]


async def test_only_unsendable_renditions_make_no_call():
    runner, llm, contexts = _runner(_all_photos)
    out = await runner.triage("user-1", "acc-1", [_image(1, data=b"")])
    assert out.results == (TriageResult("item-1", PHOTO, ""),) and out.model_calls == 0
    assert llm.requests == [] and contexts.build.call_count == 0


async def test_refused_batch_is_halved_down_to_the_refused_image():
    def handler(request):
        sent = [base64.b64decode(p.file_data["base64"]) for p in request.messages[0].parts if p.file_data]
        return LLMClientError("image rejected", http_status=400) if JPEG + bytes([3]) in sent else _all_photos(request)

    runner, llm, _ = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 5)])
    assert out.results[2] == TriageResult("item-3", PHOTO, "")
    assert [r.caption for r in out.results] == ["photo 1", "photo 2", "", "photo 4"]
    assert out.model_calls == 5   # the batch, two halves, two quarters
    assert [_indexes(r) for r in llm.requests] == [[1, 2, 3, 4], [1, 2], [3, 4], [3], [4]]


async def test_outage_raises_for_the_batch():
    runner, _, _ = _runner(lambda r: LLMServerError("529", http_status=529))
    with pytest.raises(DriveModelUnavailable):
        await runner.triage("user-1", "acc-1", [_image(1)])


async def test_missing_prompt_raises():
    runner, llm, _ = _runner(_all_photos, builder=prompt_builder(prompt=""))
    with pytest.raises(DriveModelUnavailable):
        await runner.triage("user-1", "acc-1", [_image(1)])
    assert llm.requests == []


async def test_built_from_the_users_config_and_billed_to_the_account():
    config = UserBotConfig(agent_providers={"drive_triage": "openai"})
    quota = AsyncMock()
    runner, _, contexts = _runner(_all_photos, config=config, quota=quota)
    await runner.triage("user-1", "acc-9", [_image(1)])
    contexts.build.assert_called_once_with("drive_triage", config)
    assert quota.record_usage.await_args.kwargs["account_id"] == "acc-9"


async def test_missing_user_or_provider_is_an_outage():
    runner, _, _ = _runner(_all_photos, user_missing=True)
    with pytest.raises(DriveModelUnavailable):
        await runner.triage("user-1", "acc-1", [_image(1)])
    runner, _, contexts = _runner(_all_photos)
    contexts.build.side_effect = ValueError("Provider 'openai' not registered")
    with pytest.raises(DriveModelUnavailable):
        await runner.triage("user-1", "acc-1", [_image(1)])

