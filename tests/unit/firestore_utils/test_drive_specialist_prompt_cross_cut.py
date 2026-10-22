"""Cross-cut review X2: the specialist's process line covers opening a file by its path as well as changing one."""
from pathlib import Path

TOKEN = Path(__file__).resolve().parents[3] / "prompts_snapshot/tokens/system/COGNITIVE_PROCESS_DRIVE.groovy"


def test_the_process_line_says_search_before_you_change_or_open():
    text = TOKEN.read_text(encoding="utf-8")
    assert "Search before you change or open: find the file's drive: ref" in text
