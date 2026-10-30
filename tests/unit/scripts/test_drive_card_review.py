"""Card review script (USER_DRIVE_INDEXING_RFC §26.6 D37, §26.8 S5): the shape function, grouping, distributions and
re-scoring on a synthetic snapshot. No network: the repository is a fake."""
import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.validation import drive_card_review as script
from scripts.validation import drive_card_review_logic as logic
from src.domain import drive_accuracy
from src.domain.drive_enrichment import DriveProfile, Grade, GradedValue, ProfileDate
from src.domain.drive_index import DriveIndexRecord
from src.ports.drive_index_repository import DriveIndexRepository

NIT, INT, AS = Grade.NOT_IN_TEXT.value, Grade.IN_TEXT.value, Grade.IN_TEXT_AS.value


def _rec(item, graded, *, doc_type="invoice", capture="", extracted_by="parser@3", fallback=False, truncated=False,
         refused=0, score=None, dates=()):
    profile = DriveProfile(title="t", what_it_is="w", language="en", doc_type=doc_type, dates=tuple(dates),
                           graded=tuple(graded))
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item, name=f"{item}.pdf", indexed_generation=1, profile=profile,
                            profile_source="model", capture=capture, extracted_by=extracted_by,
                            read_by_fallback=fallback, truncated=truncated, pages_refused=refused, card_accuracy=score)


def _g(field, value, grade, seen=""):
    return GradedValue(field, value, grade, seen)


def _card(rec):
    return logic.build_card(rec, rec.profile.graded)


def _snapshot():
    d = ProfileDate(label="issued", iso=date(2031, 5, 7), printed="07 05 2031")
    return [
        _rec("a", [_g("date", "2031-05-07", NIT), _g("value", "AB 12345", NIT), _g("entity", "Zorblax", INT)],
             dates=[d]),
        _rec("b", [_g("date", "2030-01-02", NIT), _g("value", "ЖК 99887", NIT), _g("entity", "Orb", INT), _g("entity", "Orb2", INT)], doc_type="passport",
             capture="scan", extracted_by="reader@2", fallback=True),
        _rec("c", [_g("date", "2029-03-04", INT), _g("entity", "Ганновер", NIT), _g("entity", "Plinth", AS)], doc_type="invoice"),
        _rec("d", [_g("value", "XY 11111", NIT)], doc_type="invoice", capture="screenshot", truncated=True),
    ]


@pytest.mark.parametrize("printed, shape", [
    ("07 05 2031", "99 99 9999"), ("07/05/2031", "99/99/9999"), ("AB 12345", "AA 99999"),
    ("Жк-99887", "AA-99999"), ("X1234567A", "A9999999A"), ("", ""), ("№ 12.5", "№ 99.9"),
])
def test_shape_of_digits_letters_any_script_separators_kept(printed, shape):
    assert logic.shape_of(printed) == shape


def test_dates_use_the_printed_form_not_the_iso():
    cards = [_card(r) for r in _snapshot()]
    a = next(c for c in cards if c.item_id == "a")
    date_value = next(v for v in a.values if v.field == "date")
    assert date_value.printed == "07 05 2031"
    assert next(v for v in a.values if v.field == "value").printed == "AB 12345"


def test_shape_groups_count_files_and_skip_graded_ok_values():
    cards = [_card(r) for r in _snapshot()]
    groups = {(g.field, g.shape): g for g in logic.shape_groups(cards)}
    assert groups[("value", "AA 99999")].files == 3          # AB 12345, ЖК 99887, XY 11111
    assert groups[("value", "AA 99999")].values == 3
    assert groups[("date", "99 99 9999")].files == 1         # a's printed form
    assert groups[("date", "9999-99-99")].files == 1         # b: no printed form stored, ISO shape
    assert ("entity", "AAAAAAAA") in groups                  # c's not_in_text entity
    assert ("entity", "AAAAAAA") not in groups   # Zorblax is in_text
    ordered = logic.shape_groups(cards)
    assert ordered[0].files == max(g.files for g in ordered)


def test_one_file_with_two_values_of_a_shape_counts_one_file():
    rec = _rec("e", [_g("value", "AB 11111", NIT), _g("value", "CD 22222", NIT)])
    (group,) = logic.shape_groups([_card(rec)])
    assert (group.files, group.values) == (1, 2)


def test_rescore_under_current_tables_equals_the_domain_formula():
    for rec in _snapshot():
        card = _card(rec)
        expected = drive_accuracy.card_accuracy(rec, rec.profile.graded)
        assert logic.rescore(card, logic.default_tables()) == expected


def test_rescore_under_a_candidate_equals_accuracy_with_those_weights(monkeypatch):
    candidate = logic.load_tables({
        "field_weights": {"date": 5, "entity": 1}, "grade_miss": {"in_text_as": 0.3},
        "source_share": {"scan": 0.2}, "reading_points": {"read_by_fallback": 10, "truncated": 1},
    })
    cards = [(r, _card(r)) for r in _snapshot()]
    got = {c.item_id: logic.rescore(c, candidate) for _, c in cards}
    monkeypatch.setattr(drive_accuracy, "FIELD_WEIGHTS", candidate.field_weights)
    monkeypatch.setattr(drive_accuracy, "GRADE_MISS", candidate.grade_miss)
    monkeypatch.setattr(drive_accuracy, "SOURCE_SHARE", candidate.source_share)
    monkeypatch.setattr(drive_accuracy, "READING_POINTS", candidate.reading_points)
    for rec, card in cards:
        assert got[card.item_id] == drive_accuracy.card_accuracy(rec, rec.profile.graded)
    assert len(set(got.values())) > 1


@pytest.mark.parametrize("raw", [
    {"field_weight": {}}, {"field_weights": {"datez": 1}}, {"grade_miss": {"maybe": 0.5}},
    {"source_share": {"scan": "high"}}, {"reading_points": {"truncated": -1}}, {"field_weights": []}, [],
])
def test_load_tables_refuses_unknown_keys_and_bad_values(raw):
    with pytest.raises(logic.WeightsError):
        logic.load_tables(raw)


def test_load_tables_omitted_keys_keep_current_values():
    t = logic.load_tables({"field_weights": {"date": 9}})
    assert t.field_weights["date"] == 9 and t.field_weights["entity"] == drive_accuracy.FIELD_WEIGHTS["entity"]
    assert t.source_share == dict(drive_accuracy.SOURCE_SHARE)


def test_lowest_cards_below_threshold_lowest_first():
    cards = [_card(r) for r in _snapshot()]
    scores = {c.item_id: logic.rescore(c, logic.default_tables()) for c in cards}
    low = logic.lowest_cards(cards, scores, 90)
    assert [scores[c.item_id] for c in low] == sorted(scores[c.item_id] for c in low)
    assert all(scores[c.item_id] < 90 for c in low)
    assert logic.lowest_cards(cards, scores, 0) == []


def test_distributions_by_every_dimension():
    cards = [_card(r) for r in _snapshot()]
    scores = {c.item_id: logic.rescore(c, logic.default_tables()) for c in cards}
    dist = logic.distributions(cards, scores)
    assert set(dist) == {"doc_type", "capture", "extractor", "fallback"}
    assert dist["doc_type"]["invoice"].count == 3 and dist["doc_type"]["passport"].count == 1
    assert dist["capture"]["scan"].count == 1 and dist["capture"]["(none)"].count == 2
    assert set(dist["extractor"]) == {"parser", "reader"}
    assert dist["fallback"]["read by fallback"].count == 1
    inv = dist["doc_type"]["invoice"]
    inv_scores = sorted(scores[c.item_id] for c in cards if c.doc_type == "invoice")
    assert (inv.minimum, inv.maximum) == (inv_scores[0], inv_scores[-1])
    assert inv.mean == pytest.approx(sum(inv_scores) / 3)


def test_compare_reports_moved_cards_and_distribution_deltas():
    cards = [_card(r) for r in _snapshot()]
    old = {c.item_id: logic.rescore(c, logic.default_tables()) for c in cards}
    tables = logic.load_tables({"reading_points": {"read_by_fallback": 20}})
    new = {c.item_id: logic.rescore(c, tables) for c in cards}
    cmp = logic.compare(cards, old, new)
    assert [d.card.item_id for d in cmp.deltas] == ["b"]
    assert cmp.deltas[0].old - cmp.deltas[0].new in range(1, 21)
    assert cmp.new_dist["fallback"]["read by fallback"].mean < cmp.old_dist["fallback"]["read by fallback"].mean


def test_report_has_all_sections_and_candidate_only_with_weights():
    cards = [_card(r) for r in _snapshot()]
    plain = script.review(cards, "u", 90)
    assert "## 1." in plain and "## 2." in plain and "## 3." in plain and "## 4." not in plain
    assert "`99 99 9999`" in plain
    withw = script.review(cards, "u", 90, logic.load_tables({"field_weights": {"date": 1}}), "cand.json")
    assert "## 4. Candidate weights (cand.json)" in withw


@pytest.mark.parametrize("out", ["/tmp/x.md", "docs/report.md", "scripts/memory/../report.md"])
def test_out_outside_scripts_memory_is_refused(out):
    with pytest.raises(SystemExit):
        script.resolve_out(Path(out), "u")


def test_out_default_and_inside_memory_accepted():
    assert script.resolve_out(None, "u").parent == (script._MEMORY_DIR).resolve()
    assert script.resolve_out(Path("scripts/memory/sub/r.md"), "u").name == "r.md"


def test_read_weights_refuses_a_bad_file_with_a_clear_error(tmp_path):
    bad = tmp_path / "w.json"
    bad.write_text(json.dumps({"field_weights": {"nope": 1}}))
    with pytest.raises(SystemExit, match="nope"):
        script.read_weights(bad)
    bad.write_text("{not json")
    with pytest.raises(SystemExit):
        script.read_weights(bad)


async def test_collect_cards_pages_and_skips_unscored_records():
    recs = _snapshot()
    folder = DriveIndexRecord(user_id="u", connection_id="u", item_id="f", name="dir", is_folder=True)
    repo = AsyncMock(spec=DriveIndexRepository)
    repo.records_page.side_effect = [recs[:2] + [folder], recs[2:], []]
    cards = await script.collect_cards(repo, "u")
    assert [c.item_id for c in cards] == ["a", "b", "c", "d"]
    assert repo.records_page.await_args_list[1].args[1] == "f"
    repo.get_chunks.assert_not_awaited()
