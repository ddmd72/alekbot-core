"""How the email agent is told to open an attachment (G3 review, slice 8): a v2 index line counts its files and names
none, so the formatter's attachment hint asks for the email id alone and says what happens with one file or several,
never the v1 hint that asked for a name (the v1 result is gone, slice 10); the manifest no longer says search results
name attachments and lets open_email_attachment take an email id alone, which the document service answers by opening
the only attachment or listing several (checked here on `_match_email_part`). Invented cast only."""
import json
import re

from src.domain.email import EmailPart
from src.infrastructure.agent_manifest import EMAIL_SEARCH, FILE_MANAGEMENT, Intent
from src.infrastructure.delegation_engine import _format_email_search_compact
from src.services.file_conversion_service import _match_email_part

V2_HINT = ("To read attachment: delegate open_email_attachment with context={\"email_id\": \"<id>\"} (an index line "
           "counts its files and does not name them: with one file it opens, with several the answer lists them to "
           "choose from)")
V1_HINT = "To read attachment: delegate open_email_attachment with context={\"email_id\": \"<id>\", \"name\": \"<filename>\"}"


def _fmt(body) -> str:
    return _format_email_search_compact(json.dumps(body), nonce=lambda: "00000000")


def _index(has_attachments: bool):
    return {"mode": "ranked", "sections": [["tool", "Indexed mail, best match first."],
                                           ["mail", "• [msg_rome01] 2025-06-02 · booking · Albergo Immaginario"]],
            "shown_ids": ["msg_rome01"], "folded_ids": {}, "has_attachments": has_attachments}


def test_a_v2_index_with_files_asks_for_the_email_id_alone():
    out = _fmt({"index": _index(True), "fresh": None, "by_key": [], "notes": []})
    assert V2_HINT in out and V1_HINT not in out


def test_a_v2_index_without_files_has_no_attachment_hint():
    out = _fmt({"index": _index(False), "fresh": None, "by_key": [], "notes": []})
    assert "To read attachment" not in out


def test_the_search_capability_no_longer_says_results_name_attachments():
    text = EMAIL_SEARCH.capability_descriptions[Intent.SEARCH_EMAILS]
    assert "index results also name their attachments" not in text
    assert text.endswith("Returns emails with their ids; get_email_details lists an email's attachments by name.")


def test_open_attachment_takes_an_email_id_alone_and_a_name_when_known():
    text = EMAIL_SEARCH.capability_descriptions[Intent.OPEN_EMAIL_ATTACHMENT]
    assert ('context={"email_id": "<id from search_emails result>"}, adding "name": "<attachment name>" when you '
            "know it") in text
    name = EMAIL_SEARCH.context_schemas[Intent.OPEN_EMAIL_ATTACHMENT]["name"]
    assert "as get_email_details or an [Email file: …] label shows it" in name
    assert "leave it out to open an email's only attachment or to get the list of its files" in name
    assert "search_emails" not in name


def test_send_file_says_search_gives_no_ref():
    texts = " ".join(str(v) for d in (FILE_MANAGEMENT,) for v in d.capability_descriptions.values())
    assert "(search_emails gives no ref)" in texts and "gives only names" not in texts


def _part(part_id: str, filename: str) -> EmailPart:
    return EmailPart(part_id=part_id, filename=filename, mime="application/pdf", size=1000, handle="h")


def test_with_no_name_the_only_attachment_opens_and_several_are_listed():
    one = [_part("1", "booking_RM-55120.pdf")]
    assert _match_email_part(one, None, None) == (one[0], [])
    two = [_part("1", "booking_RM-55120.pdf"), _part("2", "map.png")]
    part, choices = _match_email_part(two, None, None)
    assert part is None and [p.filename for p in choices] == ["booking_RM-55120.pdf", "map.png"]
    assert re.fullmatch(r"map\.png", _match_email_part(two, None, "MAP.png")[0].filename)
