"""The Cabinet's `escapeHtml` escapes all five HTML-significant characters. Fact text (facts can come from external
emails) goes through it into innerHTML; a no-op escape there is stored XSS. The static check reads the function
from the page; the behavioural check runs it under node when node is installed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"
_ATTACK = """<img src=x onerror="alert('&amp;')">"""


def _function():
    match = re.search(r"\n(\s*function escapeHtml\(str\) \{.*?\n\s*\})\n", _HTML.read_text(encoding="utf-8"), re.S)
    assert match, "escapeHtml not found"
    return match.group(1)


def test_every_html_significant_character_is_replaced_by_a_numeric_entity():
    body = _function()
    assert re.search(r"""/\[&<>"'\]/g""", body), body   # one character class covering all five
    assert '"&#"' in body and "charCodeAt" in body


def test_no_replacement_produces_a_raw_character():
    """The no-op shape: a replace whose target is the very character it matched."""
    body = _function()
    for pattern, target in re.findall(r'\.replace\(/(.)/g,\s*"(.)"\)', body):
        assert pattern != target, body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_escaped_text_carries_no_markup_and_entities_stay_literal():
    script = _function() + f"\nprocess.stdout.write(JSON.stringify([escapeHtml({json.dumps(_ATTACK)}), " \
                           "escapeHtml(null), escapeHtml(undefined), escapeHtml(0), escapeHtml('&lt;b&gt;')]));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    attack, none, undef, zero, entity = json.loads(out)
    assert not set("<>\"'") & set(attack)
    assert re.fullmatch(r"(?:[^&]|&#\d+;)*", attack), attack
    assert (none, undef, zero) == ("", "", "0")
    assert entity == "&#38;lt;b&#38;gt;"   # an entity in the source is shown as text, never decoded into a tag
