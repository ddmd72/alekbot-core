"""`DriveOperations.search(wide=…)` and `.find_passages(wide=…, batch=…)` (DRIVE_MCP_CONNECTOR_RFC §14.2-§14.4): the
connector's arguments parsed (any form of "yes" is wide, anything else the chat's caps, never an error; a batch that is
not a positive integer is a correctable request error), the caps and the batch reaching the real search service, the
outcome's `more`, and batch k rendered over the context the earlier batches showed. Without either argument the call
is the chat's. Over the real service and the in-memory index; invented items only."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.drive_excerpt_caps import ExcerptCaps
from src.domain.drive_excerpt_context import folder_line
from src.domain.drive_excerpt_request import QUESTION_REQUIRED, ExcerptRequest
from src.domain.drive_index_params import ABOUT_SHOWN, EXCERPTS_K
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_search_caps import SearchCaps
from src.domain.drive_wide_batch import BAD_BATCH
from src.infrastructure.drive_operations import DriveOperations
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.unit.services.drive_search_v2_fixture import V2World
from tests.unit.services.test_drive_search_excerpts import QUESTION, PassageWorld

CTX = {"user_id": "u", "account_id": "acc"}


def ops(service) -> DriveOperations:
    return DriveOperations(conversion_service=MagicMock(spec=FileConversionService),
                           drive_service=AsyncMock(spec=UserDriveService), drive_search=service)


def passage_world(files: int = 5, chunks: int = 5, *, noted: bool = False) -> PassageWorld:
    w = PassageWorld()
    parent = "root"
    if noted:
        parent = w.folder("R&D")
        w.note(parent, "Acme Hamburg specifications")
    for f in range(files):
        w.doc(f"Tom's plan part {f + 1}.docx", [f"Step {i + 1} of the first review, part {f + 1}." for i in range(chunks)],
              [0.05 * (f * chunks + i + 1) for i in range(chunks)], parent=parent)
    return w


def count(outcome) -> int:
    return sum(len(f.chunks) for f in outcome.excerpts.files)


# -- find_passages ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("wide, expected", [(None, EXCERPTS_K), ("", EXCERPTS_K), ("no", EXCERPTS_K),
                                            ("yes", ExcerptCaps.WIDE.k), ("TRUE", ExcerptCaps.WIDE.k),
                                            ("1", ExcerptCaps.WIDE.k)])
async def test_wide_selects_the_wide_caps_and_anything_else_the_chats(wide, expected):
    w = passage_world()
    outcome = await ops(w.service).find_passages(CTX, "u", question=QUESTION, wide=wide)
    assert outcome.cls is OutcomeClass.OK and count(outcome) == expected
    assert outcome.more is True and outcome.more is outcome.excerpts.more


async def test_batch_two_is_the_services_second_batch_and_the_last_one_has_no_more():
    w = passage_world(files=3, chunks=5)
    first = await ops(w.service).find_passages(CTX, "u", question=QUESTION)
    second = await ops(w.service).find_passages(CTX, "u", question=QUESTION, batch="2")
    direct = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=2)
    assert second.excerpts == direct and count(second) == 5 and second.more is False
    assert not {c.text for f in first.excerpts.files for c in f.chunks} & {
        c.text for f in second.excerpts.files for c in f.chunks}


@pytest.mark.parametrize("batch", ["0", "-1", "two", "1.5"])
async def test_a_batch_that_is_not_a_positive_integer_is_a_request_error_with_its_correction(batch):
    w = passage_world()
    outcome = await ops(w.service).find_passages(CTX, "u", question=QUESTION, batch=batch)
    assert outcome.cls is OutcomeClass.REQUEST_ERROR and outcome.text == BAD_BATCH.format(value=batch)
    assert w.repo.knn_limits == []   # refused before any read


async def test_a_missing_question_is_named_before_the_batch():
    outcome = await ops(passage_world().service).find_passages(CTX, "u", question="", batch="two")
    assert outcome.cls is OutcomeClass.REQUEST_ERROR and outcome.text == QUESTION_REQUIRED


async def test_batch_two_does_not_repeat_the_folder_note_batch_one_showed():
    w = passage_world(files=3, chunks=5, noted=True)
    first = await ops(w.service).find_passages(CTX, "u", question=QUESTION)
    second = await ops(w.service).find_passages(CTX, "u", question=QUESTION, batch="2")
    (note,) = first.excerpts.context.folders.values()
    assert folder_line(note) in first.text and folder_line(note) not in second.text
    assert second.excerpts.files   # the folder's files still print


async def test_without_the_connectors_arguments_the_call_is_the_chats():
    chat_world, connector_world = passage_world(), passage_world()
    chat = await ops(chat_world.service).find_passages(CTX, "u", question=QUESTION)
    plain = await ops(connector_world.service).find_passages(CTX, "u", question=QUESTION, wide="", batch="")
    assert plain.excerpts == chat.excerpts and connector_world.repo.calls == chat_world.repo.calls


# -- search -----------------------------------------------------------------------------------------------------------

def coimbra(n: int) -> V2World:
    w = V2World()
    for i in range(n):
        w.file(f"IMG_{i:04d}.jpg", kind="image", taken=f"2025-05-{i + 1:02d}", place_forms=("Coimbra",))
    return w


def about(outcome) -> int:
    return sum(1 for item in outcome.items)


async def test_a_wide_search_shows_twice_as_many_per_tier_and_says_when_a_tier_held_more():
    w = coimbra(ABOUT_SHOWN + 3)
    narrow = await ops(w.service).search(CTX, "u", query="Coimbra")
    wide = await ops(w.service).search(CTX, "u", query="Coimbra", wide="yes")
    assert about(narrow) == ABOUT_SHOWN and narrow.more is True
    assert about(wide) == ABOUT_SHOWN + 3 and wide.more is False
    assert f"(showing {ABOUT_SHOWN} of {ABOUT_SHOWN + 3})" in narrow.text


async def test_a_wide_search_past_its_own_shown_count_is_more():
    w = coimbra(SearchCaps.WIDE.about + 2)
    wide = await ops(w.service).search(CTX, "u", query="Coimbra", wide="Yes")
    assert about(wide) == SearchCaps.WIDE.about and wide.more is True


async def test_a_search_with_every_match_shown_is_not_more():
    w = coimbra(3)
    outcome = await ops(w.service).search(CTX, "u", query="Coimbra")
    assert about(outcome) == 3 and outcome.more is False
