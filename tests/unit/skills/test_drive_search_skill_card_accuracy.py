"""The drive-search skill's "Card accuracy" section (USER_DRIVE_INDEXING_RFC §26.4, D36): verbatim from the RFC (line
wraps aside), one copy of each sentence, now Smart's reading lines. Reviewer (owner decision 2026-10-28): the
specialist's token copy is no longer pinned; its wording is tuned on evals."""
import re


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


PARAGRAPH = ("A described file's result carries `card accuracy: N%`. 100% — everything in this card is confirmed by the "
             "file's own text; you can trust the card fully. The lower, the more likely the card's values contain errors.")
FIND = "\"Find\", \"which files\", \"do I have\": answer from the cards at any accuracy. Never open files for this."
VALUE = ("A question about a value in a file — a number, a date, an amount, an expiry: answer from the card when its "
         "accuracy is 90% or more. Below that, open the file and check the value before answering.")
SCAN = ("For a scan or a photo of a document, the file's text is a reading of the image; when a value matters to the "
        "user, say where you read it.")


def test_the_section_is_there_with_its_rule_lines():
    """Reviewer edit at the cut (DRIVE_SPECIALIST_RFC §5.7, §7.3): the heading went with the skill; each rule line is
    Smart's reading line, verbatim. Reviewer (owner decision 2026-10-28): the assertions that the specialist's token
    carries the paragraph, the find rule and the 90% rule verbatim, and not the scan line, were removed."""
    from src.domain import drive_answer_texts as t
    assert (t.R_ACCURACY, t.R_ACCURACY_FIND, t.R_ACCURACY_VALUE, t.R_SCAN) == (PARAGRAPH, FIND, VALUE, SCAN)


# Reviewer: test_the_inaccessible_file_bullet_is_the_sections_last_and_stands_once retired at the cut. The section and
# its order went with the skill (DRIVE_SPECIALIST_RFC §5.7); the bullet stands verbatim as Smart's bot-cannot-open
# reading line (test_drive_search_skill_inaccessible.py::test_the_skill_says_what_to_do_with_a_file_the_bot_cannot_open,
# test_drive_search_skill_access_override.py) and is delivered once, with the cards that trigger it
# (tests/unit/domain/test_drive_answer.py, the `inaccessible` reading-line cases).


def test_each_rule_sentence_has_one_copy():
    """Retargeted at the cut: the section's sentences reach Smart as reading lines (DRIVE_SPECIALIST_RFC §5.7, §7.3)."""
    from src.domain import drive_answer_texts as t
    body = _flat("\n".join(v for k, v in vars(t).items() if k.startswith("R_") and isinstance(v, str)))
    for sentence in ("answer from the cards at any accuracy", "open the file and check the value before answering",
                     "say where you read it"):
        assert body.count(sentence) == 1, sentence
