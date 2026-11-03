"""Dockerfile (main image): the world-writable check covers everything either service loads, with no exemption
(USER_FILE_PARSING_RFC §4.8): the parser child runs as an unprivileged user in the main service too."""
import re
from pathlib import Path

_TEXT = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
_INSTRUCTIONS = [ln.strip() for ln in re.sub(r"\\\n", " ", _TEXT).splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
_CHECK = next(i for i in _INSTRUCTIONS if "-perm -o+w" in i)


def test_the_check_has_no_prune_and_no_node_modules_exemption():
    assert "-prune" not in _CHECK
    assert "node_modules" not in _CHECK


def test_the_check_names_app_site_packages_and_the_chromium_trees():
    assert "find /app " in _CHECK
    assert "/usr/local/lib/python3.11/site-packages" in _CHECK
    assert "/root/.cache/ms-playwright" in _CHECK
    assert "/root/.cache/puppeteer" in _CHECK


def test_a_chmod_step_removes_the_write_bit_for_others_before_the_check():
    chmods = [n for n, i in enumerate(_INSTRUCTIONS) if i.startswith("RUN chmod -R o-w ")]
    assert chmods and chmods[0] < _INSTRUCTIONS.index(_CHECK)
    chmod = _INSTRUCTIONS[chmods[0]]
    for tree in ("/app", "site-packages", "/root/.cache/ms-playwright", "/root/.cache/puppeteer"):
        assert tree in chmod


def test_the_check_runs_after_the_source_is_copied_in():
    assert _INSTRUCTIONS.index("COPY . .") < _INSTRUCTIONS.index(_CHECK)


def test_the_comments_say_the_main_service_reads_the_parser_user_too():
    assert "Only the\n# indexer reads DRIVE_PARSER_USER" not in _TEXT
    assert "never loaded by the indexer" not in _TEXT
