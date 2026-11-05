"""U4a texts: the email workflow of PROTOCOL_AGENT_SELECTION (repo mirror and upload draft), the search-result hint,
the daily-review alert and the capabilities text name `open_email_attachment`, never the removed intent
(EXTERNAL_FILES_RFC §4.7, D11)."""
import json
import re
from pathlib import Path

from src.infrastructure.delegation_engine import _format_email_search_compact
from src.services.email_review_service import EmailReviewService
from src.utils.capabilities import CAPABILITIES_TEXT

ROOT = Path(__file__).resolve().parents[3]
MIRROR = ROOT / "prompts_snapshot" / "tokens" / "system" / "PROTOCOL_AGENT_SELECTION.groovy"
DRAFT = ROOT / "firestore_utils" / "uploads" / "PROTOCOL_AGENT_SELECTION.json"


def _texts():
    return {
        "mirror": MIRROR.read_text(encoding="utf-8"),
        "draft": json.loads(DRAFT.read_text(encoding="utf-8"))["content"],
    }


def test_both_token_copies_name_the_new_intent_and_not_the_removed_one():
    for name, text in _texts().items():
        assert "get_email_attachment" not in text, name
        assert "open_email_attachment {" in text, name
        assert "search_emails → get_email_details or open_email_attachment" in text, name


def test_the_intent_block_shows_both_inputs_and_one_attachment_per_call():
    for name, text in _texts().items():
        block = text[text.index("open_email_attachment {"):]
        block = block[: block.index("call_by_name")]
        assert "ref=email:" in block, name
        assert "email_id plus the exact attachment name" in block, name
        assert "One attachment per call" in block, name
        assert '"file_ref": "email:' in text, name
        assert '"name": "invoice_FA-0137.pdf"' in text, name


def test_the_email_block_has_no_cyrillic():
    for name, text in _texts().items():
        assert not re.search(r"[Ѐ-ӿ]", text), name


def test_the_search_result_hint_points_at_email_id_and_name():
    payload = json.dumps({"emails": [{"email_id": "msg_example001", "from": "a@example.com",
                                      "date": "2026-10-01", "attachments": ["invoice_FA-0137.pdf"]}]})
    out = _format_email_search_compact(payload)
    assert 'delegate open_email_attachment with context={"email_id": "<id>", "name": "<filename>"}' in out
    assert "get_email_attachment" not in out
    assert '"filename"' not in out


def test_the_daily_review_alert_names_the_new_intent():
    alert = EmailReviewService.build_alert("2026-01-15", 5)
    assert "open_email_attachment" in alert
    assert "get_email_attachment" not in alert


def test_capabilities_say_scans_are_read_and_files_have_references():
    assert "scanned PDFs" in CAPABILITIES_TEXT
    assert "reference" in CAPABILITIES_TEXT
