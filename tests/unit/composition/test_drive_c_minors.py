"""Adversarial review C minors (USER_DRIVE_INDEXING_RFC §24.2, §24.5, §8.1):
- one vision cap, 3.75 MiB raw, for every image the drive models are sent (its base64 stays within 5 MiB);
- a prompt assembly that build_drive_prompt rejects is not served from the cache afterwards."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.drive_model_setup import build_drive_model_extractors
from src.composition.drive_reader_runner import DriveReaderRunner
from src.composition.drive_triage_runner import DriveTriageRunner
from src.composition.prompt_assembly_setup import build_prompt_assembly_service
from src.config.environment import EnvironmentConfig
from src.domain.drive_extraction import READER, DriveModelUnavailable, ExtractionRequest, ExtractionStatus, TriageImage
from src.domain.drive_image_mime import MAX_VISION_IMAGE_BYTES
from src.domain.drive_index import DriveIndexRecord, TriageVerdict
from src.domain.drive_index_run import READER_IMAGE_MAX_BYTES, original_fallback
from src.domain.user import UserBotConfig
from src.infrastructure.drive_model_call import build_drive_prompt
from tests.unit.agents.drive_model_fakes import RoutedLLM, context, prompt_builder, reply

JPEG = b"\xff\xd8\xff\xe0"


# =============================================================================
# the vision cap
# =============================================================================

def test_the_cap_is_3_75_mib_and_its_base64_fits_5_mib():
    assert MAX_VISION_IMAGE_BYTES == 3 * 1024 * 1024 + 768 * 1024
    assert len(base64.b64encode(b"x" * MAX_VISION_IMAGE_BYTES)) <= 5 * 1024 * 1024


def test_the_original_fallback_uses_the_same_cap():
    assert READER_IMAGE_MAX_BYTES == MAX_VISION_IMAGE_BYTES
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", name="a.jpg", parent_id="root", mime_type="image/jpeg",
                           size_bytes=MAX_VISION_IMAGE_BYTES + 1)
    assert original_fallback(rec) is False


def _reader(llm):
    users = AsyncMock()
    users.get_user.return_value = SimpleNamespace(config=UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    return DriveReaderRunner(contexts, users, prompt_builder(), MagicMock()), users, contexts


@pytest.mark.parametrize("size,sent", [(MAX_VISION_IMAGE_BYTES, True), (MAX_VISION_IMAGE_BYTES + 1, False)])
async def test_reader_image_cap(tmp_path, size, sent):
    llm = RoutedLLM(lambda r: reply({"text": "TOTAL 1", "brief": None}))
    runner, _, _ = _reader(llm)
    path = tmp_path / "img"
    path.write_bytes(JPEG + b"x" * (size - len(JPEG)))
    out = await runner.extract(ExtractionRequest(path=str(path), name="i.jpg", kind="image", mime_type="image/jpeg",
                                                 user_id="u", account_id="a", max_model_pages=10))
    assert (out.status is ExtractionStatus.TOO_LARGE) is not sent and bool(llm.requests) is sent


async def test_triage_never_sends_a_thumbnail_over_the_cap():
    def handler(request):
        n = sum(1 for p in request.messages[0].parts if p.file_data)
        return reply({"images": [{"index": i, "verdict": "text", "description": "", "objects": [], "scene": "",
                                  "visible_text": ""} for i in range(1, n + 1)]})

    llm = RoutedLLM(handler)
    users = AsyncMock()
    users.get_user.return_value = SimpleNamespace(config=UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    runner = DriveTriageRunner(contexts, users, prompt_builder())
    images = [TriageImage(item_id="small", thumbnail=JPEG + b"x", mime_type="image/jpeg", camera_prior=False),
              TriageImage(item_id="big", thumbnail=JPEG + b"x" * MAX_VISION_IMAGE_BYTES, mime_type="image/jpeg",
                          camera_prior=False)]
    out = await runner.triage("u", "a", images)
    assert sum(1 for p in llm.requests[0].messages[0].parts if p.file_data) == 1
    verdicts = {r.item_id: r.verdict for r in out.results}
    # triage v2 (§25.13.1): the big one is never sent and is reported refused — no description; the tick keeps it a
    # photograph without one
    assert verdicts == {"small": TriageVerdict.TEXT} and out.refused_item_ids == ("big",)


# =============================================================================
# a rejected prompt assembly is not cached
# =============================================================================

def _prompt_db(docs):
    """A read-only fake Firestore over `docs` (doc id -> data), read at every get, for the real assembly service."""
    def collection(name):
        col = MagicMock()

        def document(doc_id):
            data = docs.get(doc_id)
            snap = MagicMock(exists=data is not None)
            snap.to_dict.return_value = data
            ref = MagicMock()
            ref.get = AsyncMock(side_effect=lambda: snap)
            return ref

        col.document.side_effect = document
        return col

    db = MagicMock()
    db.collection.side_effect = collection
    return db


def _prompt_docs():
    docs = {}
    for cls in ("cognitive_process", "output_format"):
        tid = f"{cls.upper()}_DRIVE_READER"
        docs[tid] = {"token_id": tid, "category": cls, "class": cls, "content": f'rules: "{tid}"', "metadata": {}}
    docs["drive_reader_agent_v1"] = {"blueprint_id": "drive_reader_agent_v1", "outer_class": "A extends Agent",
                                     "class_order": ["cognitive_process", "output_format"]}
    docs["drive_reader"] = {"agent_id": "drive_reader", "blueprint_id": "drive_reader_agent_v1", "tokens": {
        "COGNITIVE_PROCESS_DRIVE_READER": {"order": 10}, "OUTPUT_FORMAT_DRIVE_READER": {"order": 10}}}
    return docs


async def test_a_rejected_assembly_is_rebuilt_once_the_missing_token_is_uploaded():
    docs = _prompt_docs()
    missing = docs.pop("OUTPUT_FORMAT_DRIVE_READER")
    assembly, _ = build_prompt_assembly_service(_prompt_db(docs), EnvironmentConfig())
    extractors, _ = build_drive_model_extractors(parser=MagicMock(), users=MagicMock(), account_repo=MagicMock(),
                                                 assembly_service=assembly, keys={})
    builder = extractors[READER]._prompt_builder
    with pytest.raises(DriveModelUnavailable):
        await build_drive_prompt(builder, "drive_reader")   # incomplete: no output_format section
    docs["OUTPUT_FORMAT_DRIVE_READER"] = missing                # the owner uploads the token
    prompt = await build_drive_prompt(builder, "drive_reader")
    assert "output_format {" in prompt


async def test_a_rejection_asks_the_builder_to_drop_its_cache():
    builder = prompt_builder(prompt="class X {\n}")
    with pytest.raises(DriveModelUnavailable):
        await build_drive_prompt(builder, "drive_reader")
    builder.invalidate_cache.assert_called_once_with()


async def test_an_accepted_assembly_keeps_the_cache():
    builder = prompt_builder()
    await build_drive_prompt(builder, "drive_reader")
    builder.invalidate_cache.assert_not_called()
