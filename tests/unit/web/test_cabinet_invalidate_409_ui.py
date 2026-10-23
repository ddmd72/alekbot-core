"""Facts S2 · T7: on a 409 the Cabinet shows the server's message and reloads the list, so a stale row (a merged or
superseded id) does not stay and answer 409 on every retry (a static check of the page source)."""
import re
from pathlib import Path

_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"


def _invalidate_fact_direct():
    page = _HTML.read_text(encoding="utf-8")
    match = re.search(r"async function invalidateFactDirect\(\) \{.*?\n      \}\n", page, re.S)
    assert match
    return match.group(0)


def test_a_409_shows_the_server_message_closes_the_modal_and_reloads_the_list():
    body = _invalidate_fact_direct()
    branch = body[body.index("res.status === 409"):body.index("if (!res.ok) throw")]

    assert "await res.json()" in branch and "data.error" in branch
    assert "showToast(data.error" in branch
    assert "closeModal()" in branch
    assert "await browseInit()" in branch
    assert "return;" in branch


def test_the_409_branch_runs_before_the_generic_error_and_the_row_removal():
    body = _invalidate_fact_direct()

    assert body.index("res.status === 409") < body.index("if (!res.ok) throw") < body.index("allFacts.splice")
    assert "allFacts.splice" not in body[body.index("res.status === 409"):body.index("if (!res.ok) throw")]
