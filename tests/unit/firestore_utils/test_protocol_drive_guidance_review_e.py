"""Smart's drive guidance after review E (USER_DRIVE_INDEXING_RFC §10.4, §24.10, D13).

E-M2: no intent takes a file ref and computes, so sums are "open the file, then compute from the opened text".
E-M3: which date goes where is one distinction stated in the manifest's field descriptions; the token keeps only the
retry rule. A content match marked "(shortened)" is not quoted past its window: Smart asks for the file's passages
(§28.3 A13; it was an open before)."""
from tests.unit.firestore_utils.test_protocol_drive_guidance import _drive_block, _text, ser


def _block():
    # the rules these tests pin are Smart's reading lines (DRIVE_SPECIALIST_RFC §5.7). Reviewer (owner decision
    # 2026-10-28): the specialist's PROTOCOL_DRIVE_SEARCH half is no longer read; its wording is tuned on evals.
    from tests.unit.firestore_utils.test_protocol_drive_guidance import _reading_lines
    return _reading_lines()


def test_sums_open_the_file_then_compute_from_the_opened_text():
    block = _block()
    assert "open the file, then compute from the opened text" in block
    assert "compute intent" not in block and "file ref" not in block


def test_the_dates_line_is_the_retry_rule_only():
    block = _block()
    # Reviewer (owner decision 2026-10-28): the retry sentence (final review F5-4) lives only in the specialist's token,
    # so its positive assertion went; the date subtleties stay out of Smart's reading lines.
    for subtlety in ("the model read", "document's own date", "file's date", "drive_date_from"):
        assert subtlety not in block, subtlety


def test_a_shortened_passage_asks_for_its_passages_before_quoting_more():
    # Search v2 P6 wording, as USER_DRIVE_INDEXING_RFC §28.3 A13 changed it (reviewer): the card's window is "a search
    # window", and more than it shows is asked for as the file's passages instead of an open.
    assert ("A card's passage is a search window cut from the file around what matched. For more than it shows, ask "
            "for the file's passages on that point" in _block())
