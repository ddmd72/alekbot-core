"""The `low_accuracy_image` read fact of a search (USER_DRIVE_INDEXING_RFC §28 OQ-A2, owner 2026-10-28; plan S2.1):
a shown card below the trust bar (90%) whose text is not known to be born-digital (its capture is a scan, a photo of
a document, a screenshot, or none recorded) sets it; a digital card, a card at or above the bar and a card with no
accuracy (a photograph) do not. The search service is a spec mock; cards are built by hand (invented paths)."""
from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.drive_index_params import CARD_ACCURACY_TRUST
from src.domain.drive_search import STATUS_OK, DriveSearchCard, DriveSearchRequest, DriveSearchResult
from src.domain import drive_ref
from src.infrastructure.drive_operations import DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

CTX = {"user_id": "u1", "account_id": "a1"}


def _card(item_id: str, *, capture: str, accuracy: Optional[int], kind: str = "pdf") -> DriveSearchCard:
    return DriveSearchCard(ref=drive_ref.format("a1", item_id), item_id=item_id, name=f"{item_id}.pdf",
                           path=f"Contracts/{item_id}.pdf", kind=kind, is_folder=False, summary=f"{item_id}.pdf",
                           capture=capture, card_accuracy=accuracy, nonce=f"nonce{item_id.replace('_', '')}")


async def _facts(*cards: DriveSearchCard):
    search = AsyncMock(spec=DriveSearchService)
    search.search.return_value = DriveSearchResult(status=STATUS_OK, cards=cards,
                                                   request=DriveSearchRequest(query="lease"), ref_tag="a1")
    ops = DriveOperations(conversion_service=MagicMock(spec=FileConversionService),
                          drive_service=AsyncMock(spec=UserDriveService), drive_search=search)
    out = await ops.search(CTX, "u1", query="lease")
    assert out.success and out.facts.cards == len(cards)   # rendered, not a failure's empty facts
    return out.facts


async def test_a_digital_card_below_the_bar_is_not_an_image_reading():
    facts = await _facts(_card("lease", capture="digital", accuracy=CARD_ACCURACY_TRUST - 1))
    assert facts.low_accuracy_image is False and facts.accuracy is True


@pytest.mark.parametrize("capture", ["scan", "document_photo", "screenshot", ""])
async def test_an_image_reading_or_an_unknown_capture_below_the_bar_sets_the_fact(capture):
    facts = await _facts(_card("scan_0012", capture=capture, accuracy=CARD_ACCURACY_TRUST - 1))
    assert facts.low_accuracy_image is True


@pytest.mark.parametrize("capture", ["scan", "document_photo", "screenshot", "", "digital"])
@pytest.mark.parametrize("accuracy", [CARD_ACCURACY_TRUST, 100])
async def test_any_capture_at_or_above_the_bar_does_not(capture, accuracy):
    facts = await _facts(_card("scan_0012", capture=capture, accuracy=accuracy))
    assert facts.low_accuracy_image is False


async def test_a_photo_card_with_no_accuracy_does_not():
    facts = await _facts(_card("garden", capture="", accuracy=None, kind="image"))
    assert facts.low_accuracy_image is False and facts.image is True


async def test_one_low_scan_among_trusted_cards_is_enough():
    facts = await _facts(_card("lease", capture="digital", accuracy=60),
                         _card("contract", capture="scan", accuracy=95),
                         _card("scan_0012", capture="scan", accuracy=70))
    assert facts.low_accuracy_image is True


async def test_no_cards_no_fact():
    assert (await _facts()).low_accuracy_image is False
