"""A card scored below the trust bar keeps its marks and the owner's words, but not its description: the description
is the very reading the card was graded unreliable on (USER_DRIVE_INDEXING_RFC §28.3 A10, A18, OQ-A2)."""
import pytest

from src.domain.drive_excerpt_context import ExcerptContext
from src.domain.drive_excerpt_file_context import FileContext
from src.domain.drive_excerpt_folder_note import FolderNote
from src.domain.drive_excerpt_ledger import ExcerptLedger
from src.domain.drive_excerpt_render import render_excerpts
from src.domain.drive_index_params import CARD_ACCURACY_TRUST
from tests.unit.domain.drive_excerpt_builders import TOMS_PLAN, chunk, excerpt_file, nonces, result

DESC = "Tom Baker's plan for the Acme R&D review"
OWN = "noted by the owner: «Tom's draft, not final»"
RD = FolderNote("f-rd", "R&D", 1, "noted by the owner: «Acme specifications; the review process lives here»")


def _render(accuracy):
    file = excerpt_file("item-1", TOMS_PLAN, [chunk("item-1", 0, "The review starts.")], card_accuracy=accuracy)
    ctx = ExcerptContext(files=(FileContext("item-1", TOMS_PLAN, ("f-rd",), DESC, OWN),), folders={"f-rd": RD})
    return render_excerpts(result(file, context=ctx), nonce=nonces())


@pytest.mark.parametrize("accuracy", [CARD_ACCURACY_TRUST - 1, 40, 0])
def test_below_the_bar_no_description(accuracy):
    rendered = _render(accuracy)
    assert "what it is (read from the file)" not in rendered.text and DESC not in rendered.text
    assert f" · card accuracy {accuracy}%]" in rendered.text
    assert OWN in rendered.text and 'folder "R&D": ' in rendered.text and "The review starts." in rendered.text
    assert "item-1" in rendered.plan.described and rendered.plan.has_owner_line is True


@pytest.mark.parametrize("accuracy", [CARD_ACCURACY_TRUST, 100, None])
def test_at_the_bar_or_unscored_the_description_stays(accuracy):
    rendered = _render(accuracy)
    assert f"what it is (read from the file): {DESC}" in rendered.text
    assert "card accuracy" not in rendered.text


def test_the_block_for_the_orchestrator_drops_it_too():
    low = _render(70)
    ledger = ExcerptLedger()
    file = excerpt_file("item-1", TOMS_PLAN, [chunk("item-1", 0, "The review starts.")], card_accuracy=70)
    ctx = ExcerptContext(files=(FileContext("item-1", TOMS_PLAN, ("f-rd",), DESC, OWN),), folders={"f-rd": RD})
    ledger.add(result(file, context=ctx), low.plan)
    block = ledger.block(nonce=nonces())
    assert DESC not in block and OWN in block
