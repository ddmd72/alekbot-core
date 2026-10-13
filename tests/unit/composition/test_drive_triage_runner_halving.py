"""DriveTriageRunner halves a batch only for a refusal or the output cap (R3 F1-6, USER_DRIVE_INDEXING_RFC §24.5,
§24.8; triage v2 §25.4.4): a 400/422 or a 413 splits the batch down to the refused image (reported refused); a reply
stopped at the output cap splits it down to the image (an image still stopped alone is reported capped); output still
invalid after the parse retry leaves the batch's images without a result, for the caller's per-file retry, after at
most the one call and its parse retry — never up to 2n-1 calls (or 4n-2 with parse retries) for an n-image batch."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.composition.drive_triage_runner import DriveTriageRunner
from src.domain.drive_index import TriageVerdict
from src.domain.exceptions import LLMClientError
from src.domain.llm import FinishReason
from src.domain.user import UserBotConfig
from tests.unit.agents.drive_model_fakes import RoutedLLM, context, prompt_builder, reply
from src.domain.drive_enrichment import PhotoDescription
from src.domain.drive_enrichment_calls import ModelReplyStatus, TriageResultV2
from src.domain.drive_extraction import TriageImage
from src.domain.drive_index_params import TRIAGE_BATCH

JPEG = b"\xff\xd8\xff\xe0"


def _image(n):
    return TriageImage(item_id=f"item-{n}", thumbnail=JPEG + bytes([n]), mime_type="image/jpeg", camera_prior=False)


def _indexes(request):
    return [int(p.text.split()[1].rstrip(":")) for p in request.messages[0].parts
            if p.text and p.text.startswith("Image ")]


def _sent(request):
    return [base64.b64decode(p.file_data["base64"]) for p in request.messages[0].parts if p.file_data]


def _entry(i, verdict="photo", description=""):
    return {"index": i, "verdict": verdict, "description": description, "objects": [], "scene": "",
            "visible_text": ""}


def _runner(handler):
    llm = RoutedLLM(handler)
    users = AsyncMock()
    users.get_user.return_value = SimpleNamespace(config=UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    return DriveTriageRunner(contexts, users, prompt_builder()), llm


async def test_invalid_output_after_the_parse_retry_does_not_halve_the_batch():
    runner, llm = _runner(lambda request: reply(text="not json at all"))
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, TRIAGE_BATCH + 1)])
    assert len(llm.requests) == 2                         # the call and its parse retry, nothing more
    assert [_indexes(r) for r in llm.requests][0] == list(range(1, TRIAGE_BATCH + 1))
    assert out.results == ()                              # no verdict: each image goes to the per-file retry
    assert (out.capped_item_ids, out.refused_item_ids) == ((), ())
    assert out.model_calls == 2 and out.status is ModelReplyStatus.INVALID   # charged, no backoff (§25.4.6)


async def test_a_stopped_answer_is_halved_down_to_the_image():
    """§25.4.4: a batch stopped at the output cap is split and asked again; an image still stopped alone gets no
    description (`capped_item_ids`) and the batch is `stopped_at_cap` (charged, no backoff)."""
    runner, llm = _runner(lambda request: reply(text="{", finish=FinishReason.MAX_TOKENS))
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 5)])
    assert [_indexes(r) for r in llm.requests] == [[1, 2, 3, 4], [1, 2], [1], [2], [3, 4], [3], [4]]
    assert out.results == () and out.model_calls == 7
    assert out.capped_item_ids == ("item-1", "item-2", "item-3", "item-4") and out.refused_item_ids == ()
    assert out.status is ModelReplyStatus.STOPPED_AT_CAP


async def test_a_stopped_half_keeps_the_answered_halfs_results():
    def handler(request):
        indexes = _indexes(request)
        if indexes == [1, 2, 3, 4] or indexes == [3, 4]:
            return reply(text="{", finish=FinishReason.MAX_TOKENS)
        return reply({"images": [_entry(i, description=f"photo {i}") for i in indexes]})

    runner, llm = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 5)])
    assert [_indexes(r) for r in llm.requests] == [[1, 2, 3, 4], [1, 2], [3, 4], [3], [4]]
    assert [r.item_id for r in out.results] == ["item-1", "item-2", "item-3", "item-4"]
    assert out.capped_item_ids == () and out.status is ModelReplyStatus.OK


async def test_a_half_that_fails_otherwise_keeps_the_other_halfs_verdicts():
    # the batch is refused (400) because of image 3; the half [1, 2] then answers garbage: it is not halved again
    def handler(request):
        indexes = _indexes(request)
        if JPEG + bytes([3]) in _sent(request):
            return LLMClientError("image rejected", http_status=400)
        if indexes == [1, 2]:
            return reply(text="garbage")
        return reply({"images": [_entry(i, description=f"photo {i}") for i in indexes]})

    runner, llm = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(n) for n in range(1, 5)])
    assert [_indexes(r) for r in llm.requests] == [[1, 2, 3, 4], [1, 2], [1, 2], [3, 4], [3], [4]]
    assert out.results == (TriageResultV2("item-4", TriageVerdict.PHOTO, PhotoDescription(description="photo 4")),)
    assert out.refused_item_ids == ("item-3",)            # refused alone: no description (§25.13.1)
    assert out.status is ModelReplyStatus.INVALID         # items 1 and 2 are unsettled and retried


async def test_a_too_large_batch_is_still_halved():
    def handler(request):
        if JPEG + bytes([2]) in _sent(request):
            return LLMClientError("request too large", http_status=413)
        return reply({"images": [_entry(i, "text") for i in _indexes(request)]})

    runner, llm = _runner(handler)
    out = await runner.triage("user-1", "acc-1", [_image(1), _image(2)])
    assert [_indexes(r) for r in llm.requests] == [[1, 2], [1], [2]]
    assert out.results == (TriageResultV2("item-1", TriageVerdict.TEXT, None),)
    assert out.refused_item_ids == ("item-2",) and out.status is ModelReplyStatus.OK
