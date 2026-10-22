"""The drive-search system skill's rules after the cut (DRIVE_SPECIALIST_RFC §5.7, §7.3): the skill is gone, its rules
split by who acts on them — the specialist's half is the PROTOCOL_DRIVE_SEARCH token, Smart's half reaches Smart with
the results as reading lines (src/domain/drive_answer_texts.py). The owner's strategy still holds: "find" is answered
from the results, files are opened only when their contents are needed, many results are counted and grouped, never
opened one by one. The skill itself and the protocol's pointer to it are pinned gone by
test_drive_specialist_removed_names.py and test_protocol_drive_specialist.py."""
from pathlib import Path

from src.domain import drive_answer_texts as t

ROOT = Path(__file__).resolve().parents[3]
TOKEN = ROOT / "prompts_snapshot" / "tokens" / "system" / "PROTOCOL_DRIVE_SEARCH.groovy"


def _specialist() -> str:
    """The specialist's half: the token's content (after the mirror's frontmatter)."""
    return TOKEN.read_text(encoding="utf-8").split("\n---\n", 1)[1]


def _smart() -> str:
    """Smart's half: every reading line the answer can carry."""
    return "\n".join(v for k, v in vars(t).items() if k.startswith("R_") and isinstance(v, str))


def test_find_is_answered_from_the_results_and_files_open_only_for_their_contents():
    smart, specialist = _smart(), _specialist()
    assert "Answer from the search results alone" in smart
    assert "Do not open files to confirm them." in smart
    assert "then open that file. Open only the files the answer needs." in specialist
    assert "Never open them one by one to describe them." in smart
    assert "Open a photo only when the user wants to see it" in specialist


def test_the_moved_guidance_is_in_the_skill():
    smart, specialist = _smart(), _specialist()
    for line in ("'About' and 'mentioned in' are exact", "'Related by meaning only' lines are guesses",
                 "so you can tell whether the word was phrasing or the subject"):
        assert line in smart and line in specialist, line      # reading the results: both halves (§5.7)
    for line in ("\"Coverage: complete\" makes a not-found reliable", "never say a document does not exist",
                 "For sums, totals and comparisons, open the file", "never pick silently",
                 "a document is authoritative", "Before quoting more than it shows, open the file.",
                 "quote the passage and say it is an excerpt"):
        assert line in smart, line
    for line in ("When a folder card is shown under About", "search again without the filter",
                 "open the file (it is read live)"):
        assert line in specialist, line
