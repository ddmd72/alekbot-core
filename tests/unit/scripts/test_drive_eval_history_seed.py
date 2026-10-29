"""The evals seed history in the shape production stores: the drive routing probe's `_pair` and specialist_ab's
`history_pair` render `drive_context` through the handler's own `history_context_block` (one line per file, the ref
kept), never the former JSON block. Invented items only."""
from scripts.validation import drive_routing_probe as probe
from scripts.validation.drive_eval.specialist_ab import history_pair
from src.domain.history_context_block import history_context_block

ENTRIES = [[{"path": "Home/Lisbon lease.pdf", "ref": "drive:def456", "op": "searched", "query": "lease", "count": 1},
            {"path": "Home/Lisbon lease.pdf", "ref": "drive:def456", "op": "opened"}]]
LINE = '[Drive: Home/Lisbon lease.pdf ref=drive:def456 — searched for "lease" (1 file); opened whole]'


def test_specialist_ab_history_pair_renders_the_drive_lines_in_full_text_only():
    user, model = history_pair("find my lease", "One lease.", {"drive_context": ENTRIES, "drive_status": "ok"})
    assert model.parts[0].text == "One lease."
    assert model.parts[0].full_text == "One lease.\n\n" + LINE
    assert '"drive_context"' not in model.parts[0].full_text


def test_the_routing_probe_seed_is_the_handler_rendering():
    user, model = probe._pair("what is in the folder?", "Two files.", probe._LISTED)
    assert model.parts[0].text == "Two files.\n\n" + history_context_block("drive_context", probe._LISTED)
    assert model.parts[0].text.endswith("ref=drive:AAA — listed]\n[Drive: Договори/lease_2025.pdf ref=drive:BBB — listed]")
    assert probe._pair("open it", "Done.")[1].parts[0].text == "Done."
