"""Smart's drive guidance after the final review (USER_DRIVE_INDEXING_RFC §10.4, §24.10; final review F5-1, F5-4,
F5-8). One line per rule, short, consistent with the per-result facts `render_results` shows."""
from tests.unit.firestore_utils.test_protocol_drive_guidance import _drive_block, _text, ser


def _block():
    # the rules these tests pin are the specialist's PROTOCOL_DRIVE_SEARCH token and Smart's reading lines now
    # (DRIVE_SPECIALIST_RFC §5.7)
    from tests.unit.firestore_utils.test_protocol_drive_guidance import _skill_body
    return _skill_body()


def test_f5_1_an_unopenable_file_is_quoted_as_an_excerpt():
    assert ("If the file cannot be opened (HEIC, an image over 5 MB, a PDF over 20 MB), quote the passage and say it "
            "is an excerpt.") in _block()


def test_f5_4_the_date_retry_fires_when_the_file_is_not_shown():
    block = _block()
    assert "If a date-filtered search does not show the file asked for, search again without the filter." in block
    assert "If a date filter finds nothing" not in block


def test_f5_8_the_folder_rule_needs_a_name_match():
    # Search v2 P6: the About tier is the name match
    assert "When a folder card is shown under About (its name holds the words)" in _block()
