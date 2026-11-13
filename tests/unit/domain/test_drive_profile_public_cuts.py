"""The two public cuts of drive_profile change nothing for the drive: they are the private ones."""
from src.domain import drive_profile as dp

LONG = "\n\n".join(f"Section {i}. " + "text " * 60 for i in range(60))
SHEET = "## Budget\n| a | b |\n| --- | --- |\n" + "\n".join(f"| {i} | v{i} |" for i in range(40))


def test_head_and_tail_equals_the_private_cut():
    for text in (LONG, "short text", ""):
        assert dp.head_and_tail(text, 6_000, 1_500) == dp._head_tail(text, 6_000, 1_500)


def test_sheet_head_equals_the_first_part_of_the_sheet_rule():
    assert dp.sheet_head(SHEET) == dp._sheets(SHEET)[0] and dp.sheet_head(SHEET)
