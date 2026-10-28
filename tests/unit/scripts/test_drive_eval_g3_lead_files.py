"""The drive specialist eval's lead files on the G3 overlay (DRIVE_SPECIALIST_RFC §15, research 09): a lead in a card
(the locksmith note names its invoice by number) and a lead only inside an opened file (the garage agreement names its
annex in its last clause). Synthetic, rendered from templates with hand-written profiles; only G3 sees them."""
import json
from pathlib import Path

from scripts.validation.drive_eval.fixture_drive import item_id_for
from scripts.validation.drive_eval.fixture_texts_v2 import bulk_profile
from scripts.validation.drive_eval.smart_dry_run import g3_drive

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"
_LEADS = {
    "Home/Locksmith contacts.md": ("lead_locksmith_note", "Factura FA-0137"),
    "Home/Factura FA-0137.md": ("lead_factura_fk", "249,00 EUR"),
    "Contracts/Garage rental 2026.md": ("lead_garage_rental", "Normas Oak Street"),
    "Contracts/Normas Oak Street.md": ("lead_normas", "48 horas"),
}


def _drive():
    return g3_drive(_DIR / "tree.json", _DIR / "tree_g3.json", _DIR / "tree_v2.json")


def test_the_four_lead_files_are_on_the_g3_drive_only():
    paths = set(_drive().paths())
    assert set(_LEADS) <= paths
    tree = (_DIR / "tree.json").read_text(encoding="utf-8")
    assert not any(name in tree for name in ("Locksmith", "FA-0137", "Garage rental", "Normas"))


def test_each_lead_file_renders_its_text_and_has_a_hand_written_profile():
    drive = _drive()
    for path, (template, marker) in _LEADS.items():
        text = drive._content[item_id_for(path)].decode("utf-8")
        assert marker in text, path
        assert bulk_profile(template, 1) is not None, template


def test_the_card_lead_is_a_key_value_and_the_annex_never_says_garage_in_english():
    locksmith = bulk_profile("lead_locksmith_note", 1)
    assert any(kv.canonical == "FA-0137" for kv in locksmith.key_values)
    annex = _drive()._content[item_id_for("Contracts/Normas Oak Street.md")].decode("utf-8").casefold()
    assert not any(word in annex for word in ("garage", "rules", "visitor", "guest"))
    agreement = bulk_profile("lead_garage_rental", 1)
    assert "Normas" not in json.dumps([agreement.title, agreement.what_it_is, list(agreement.topics)])
