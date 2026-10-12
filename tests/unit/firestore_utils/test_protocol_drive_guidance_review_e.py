"""Smart's drive guidance after review E (USER_DRIVE_INDEXING_RFC §10.4, §24.10, D13).

E-M2: no intent takes a file ref and computes, so sums are "open the file, then compute from the opened text".
E-M3: which date goes where is one distinction stated in the manifest's field descriptions; the token keeps only the
retry rule. A content match marked "(shortened)" is opened before it is quoted."""
from tests.unit.firestore_utils.test_protocol_drive_guidance import _drive_block, _text, ser


def _block():
    return _drive_block(ser.token_from_file(_text())["content"])


def test_sums_open_the_file_then_compute_from_the_opened_text():
    block = _block()
    assert "open the file, then compute from the opened text" in block
    assert "compute intent" not in block and "file ref" not in block


def test_the_dates_line_is_the_retry_rule_only():
    block = _block()
    assert 'dates: "If a date filter finds nothing, search again without it before saying not found."' in block
    for subtlety in ("the model read", "document's own date", "file's date", "drive_date_from"):
        assert subtlety not in block, subtlety


def test_a_shortened_passage_is_opened_before_quoting():
    assert "If a passage is marked shortened, open the file before quoting from it." in _block()
