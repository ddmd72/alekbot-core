"""httpx request lines carry the Telegram bot token in the URL; they must never be logged."""
import ast
import logging
from pathlib import Path

import pytest

from src.utils.logger import quiet_third_party_loggers

pytestmark = pytest.mark.datafile

ROOT = Path(__file__).resolve().parents[3]
ENTRY_POINTS = ["main.py", "indexer_main.py", "relay_main.py", "job_main.py"]


def test_helper_sets_httpx_and_httpcore_to_warning():
    for n in ("httpx", "httpcore"):
        logging.getLogger(n).setLevel(logging.NOTSET)
    quiet_third_party_loggers()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_httpx_info_record_not_emitted(caplog):
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    quiet_third_party_loggers()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").info('HTTP Request: POST https://api.telegram.org/botSECRET/sendMessage "200 OK"')
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_entry_point_calls_helper(entry):
    tree = ast.parse((ROOT / entry).read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "quiet_third_party_loggers"]
    assert calls, f"{entry} does not call quiet_third_party_loggers()"
