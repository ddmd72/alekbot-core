"""The §26 / D39 / D40 additions to the drive eval (USER_DRIVE_INDEXING_RFC §26.8 Eval): an inaccessible item in the
fixture drive, the `expect_inaccessible` case rule, the capture fields of the snapshot and the parse cache, the G3
`must_open` / `must_not_open` hard checks, and the synthetic fixture entries."""
import json
from datetime import date
from pathlib import Path

import pytest

from scripts.validation.drive_eval.enrichment_replay import ReplayTriage
from scripts.validation.drive_eval.enrichment_snapshot import (
    EnrichmentSnapshot, outcome_from_json, outcome_to_json, photo_key, profile_from_json, profile_to_json,
)
from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from scripts.validation.drive_eval.fixture_texts_s26 import ENRICHMENT
from scripts.validation.drive_eval.parse_cache import ParseCache
from scripts.validation.drive_eval.search_v2_eval import v2_drive
from scripts.validation.drive_eval.smart_dry_run import G3Error, hard_failures, validate_g3
from scripts.validation.drive_eval.v2_metrics import GoldError, case_pass, validate_gold_v2
from src.domain.drive_capture import CaptureSignals
from src.domain.drive_enrichment import DriveProfile, ProfileDate
from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest, ExtractionStatus, TriageImage
from src.domain.drive_index import TriageVerdict
from src.domain.user_drive import DriveItemNotFoundError

from ..services.drive_content_fakes import FakeExtractor

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"
CLOSED = "Shared/Chimney inspection certificate.pdf"
SCAN = "Documents/DOC_20260610_0042.pdf"
USER = "u"


# -- the fixture drive: an inaccessible item ---------------------------------------------------------------------------

def _closed_drive() -> FixtureDrive:
    return FixtureDrive({"items": [
        {"path": "Shared/closed.pdf", "mime": "application/pdf", "size": 10, "inaccessible": True},
        {"path": "Shared/open.pdf", "mime": "application/pdf", "size": 10}]})


async def test_an_inaccessible_item_is_listed_by_delta_but_404s_on_every_id_call(tmp_path):
    drive = _closed_drive()
    listed = {c.item_id for c in (await drive.get_changes(USER, None)).changes}
    closed, opened = item_id_for("Shared/closed.pdf"), item_id_for("Shared/open.pdf")
    assert {closed, opened} <= listed and drive.is_inaccessible("Shared/closed.pdf")
    assert not drive.is_inaccessible("Shared/open.pdf")
    for call in (drive.get_change(USER, closed), drive.get_item(USER, closed), drive.download(USER, closed),
                 drive.download_to_file(USER, closed, str(tmp_path / "x"), 100), drive.get_thumbnail(USER, closed, 512)):
        with pytest.raises(DriveItemNotFoundError):
            await call
    assert (await drive.get_change(USER, opened)).item_id == opened   # an ordinary item answers


def test_the_overlay_has_one_inaccessible_item_with_bytes():
    drive = v2_drive(_DIR / "tree.json", _DIR / "tree_v2.json")
    assert [p for p in drive.paths() if drive.is_inaccessible(p)] == [CLOSED]
    assert drive.has_content(CLOSED)   # the fixture check "every file the indexer would read has bytes" holds


# -- the case rule -----------------------------------------------------------------------------------------------------

def _case(**kw):
    return {"id": "c", "family": "exact", "query": "chimney", "expected_paths": [CLOSED], "expect_tier": "about", **kw}


def test_expect_inaccessible_needs_the_access_line_on_the_card_as_well_as_the_tier():
    case = _case(expect_inaccessible=True)
    assert case_pass(case, {"tiers": {CLOSED: "about"}, "inaccessible": [CLOSED]})
    assert not case_pass(case, {"tiers": {CLOSED: "about"}, "inaccessible": []})   # found, but no access line
    assert not case_pass(case, {"tiers": {}, "inaccessible": [CLOSED]})            # the line without the tier
    assert case_pass(_case(), {"tiers": {CLOSED: "about"}})                         # a plain case is unchanged


def test_validate_refuses_expect_inaccessible_that_is_not_true_or_is_on_a_negative():
    for bad in (_case(expect_inaccessible=False), _case(family="negative", expected_paths=[], expect_inaccessible=True)):
        with pytest.raises(GoldError, match="expect_inaccessible"):
            validate_gold_v2([bad], [CLOSED])


def test_the_new_gold_cases_name_files_of_the_overlay_and_set_the_inaccessible_one():
    gold = json.loads((_DIR / "gold_v2.json").read_text(encoding="utf-8"))
    validate_gold_v2(gold, v2_drive(_DIR / "tree.json", _DIR / "tree_v2.json").paths())
    by_id = {c["id"]: c for c in gold}
    assert by_id["ex-inaccessible-name"]["expect_inaccessible"] is True
    for cid in ("cls-scans-en", "cls-scans-uk", "cls-scans-es", "cls-screenshot-ru", "ex-date-expiry-dotted",
                "ex-date-expiry-spaced", "ex-value-id-number", "cls-sunsets-en", "cls-sunsets-ru", "cls-beach-ru"):
        assert cid in by_id, cid


# -- the snapshot and the parse cache ---------------------------------------------------------------------------------

def test_a_profile_date_keeps_its_printed_form_through_the_snapshot():
    profile = DriveProfile(title="t", what_it_is="w", language="en", doc_type="id_document",
                           dates=(ProfileDate(label="valid_until", iso=date(2031, 5, 7), printed="07 05 2031"),))
    assert profile_from_json(json.loads(json.dumps(profile_to_json(profile)))) == profile
    legacy = profile_to_json(profile)
    del legacy["dates"][0]["printed"]   # a snapshot recorded before §26.4
    assert profile_from_json(legacy).dates[0].printed == ""


def test_an_outcome_keeps_the_capture_signals_and_the_capture_and_a_plain_one_adds_no_key():
    signals = CaptureSignals(pages=1, text_pages=0, raster_pages=1, median_dpi=200.0, page_short_mm=139.7,
                             page_long_mm=190.5, exif_camera=False)
    outcome = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, capture_signals=signals, capture="scan",
                                capture_source="detected")
    back = outcome_from_json(json.loads(json.dumps(outcome_to_json(outcome))))
    assert (back.capture_signals, back.capture, back.capture_source) == (signals, "scan", "detected")
    plain = outcome_to_json(ExtractionOutcome(status=ExtractionStatus.TEXT, text="x"))
    assert "capture_signals" not in plain and "capture" not in plain   # a recorded reader outcome keeps its bytes


async def test_a_parse_cache_entry_written_before_the_capture_signals_is_a_miss(tmp_path):
    source = tmp_path / "a.pdf"
    source.write_bytes(b"%PDF")
    request = ExtractionRequest(path=str(source), name="a.pdf", kind="pdf", mime_type="application/pdf", user_id="u",
                                account_id="a")
    inner = FakeExtractor(ExtractionOutcome(status=ExtractionStatus.TEXT, text="x"))
    cache_dir = tmp_path / "cache"
    first = ParseCache(inner, cache_dir)
    await first.extract(request)
    (entry,) = list(cache_dir.glob("*.json"))
    old = json.loads(entry.read_text(encoding="utf-8"))
    del old["cap"]                      # what an earlier version of the cache wrote
    entry.write_text(json.dumps(old), encoding="utf-8")
    again = ParseCache(inner, cache_dir)
    await again.extract(request)
    assert again.misses == 1 and again.hits == 0
    assert json.loads(entry.read_text(encoding="utf-8"))["cap"] == 1   # rewritten with the marker


async def test_replayed_triage_gives_a_text_image_the_capture_the_model_said(tmp_path):
    snapshot = EnrichmentSnapshot()
    snapshot.photos[photo_key(b"shot", False)] = {"paths": ["s.png"], "verdict": "text", "photo": None,
                                                  "capture": "screenshot"}
    snapshot.photos[photo_key(b"plain", False)] = {"paths": ["p.png"], "verdict": "text", "photo": None}
    reply = await ReplayTriage(snapshot).triage(USER, "a", [
        TriageImage(item_id="s", thumbnail=b"shot", mime_type="image/png", camera_prior=False),
        TriageImage(item_id="p", thumbnail=b"plain", mime_type="image/png", camera_prior=False)])
    assert {r.item_id: (r.verdict, r.capture) for r in reply.results} == {
        "s": (TriageVerdict.TEXT, "screenshot"), "p": (TriageVerdict.TEXT, "")}


def test_the_synthetic_entries_are_in_the_snapshot_marked_and_cover_every_new_file():
    snapshot = EnrichmentSnapshot.load(_DIR / "enrichment.json")
    marked = {name: [v for v in getattr(snapshot, name).values() if v.get("synthetic")]
              for name in ("profiles", "hints", "photos", "reader")}
    assert snapshot.meta["synthetic_entries"] == sum(len(v) for v in marked.values())
    assert {e["path"] for e in marked["profiles"]} == {p for p, v in ENRICHMENT.items() if "profile" in v}
    assert {e["path"] for e in marked["reader"]} == {p for p, v in ENRICHMENT.items() if "reader" in v}
    assert {e["paths"][0] for e in marked["photos"]} == {p for p, v in ENRICHMENT.items() if "photo" in v}
    assert any(e.get("capture") == "screenshot" for e in marked["photos"])


def test_the_scan_card_carries_one_misread_value_so_its_accuracy_is_under_ninety():
    printed = {d["printed"] for d in ENRICHMENT[SCAN]["profile"]["dates"]}
    text = ENRICHMENT[SCAN]["reader"]["text"]
    assert "07 05 2031" in printed and "07 05 2031" in text
    assert "07 01 1980" in printed and "07 01 1980" not in text and "01 01 1980" in text   # the misread birth date


# -- G3 ----------------------------------------------------------------------------------------------------------------

def _g3(**kw):
    return {"id": "x", "set": "positive", "category": "content", "lang": "en", "text": "t", "expect": "none", **kw}


def _gold():
    base = json.loads((_DIR / "gold_g3.json").read_text(encoding="utf-8"))
    return base + json.loads((_DIR / "gold_g3_v2.json").read_text(encoding="utf-8"))


def test_the_g3_gold_carries_one_must_open_and_one_must_not_open_case_on_the_scan():
    gold = _gold()
    validate_g3(gold)
    assert any(c.get("must_open") for c in gold) and any(c.get("must_not_open") for c in gold)


def test_validate_g3_refuses_a_contradiction_and_a_flag_that_is_not_true():
    base = [c for c in _gold() if not c.get("must_open") and not c.get("must_not_open")]
    for bad, word in ((_g3(must_open=True, must_not_open=True), "contradict"), (_g3(must_open=False), "must_open"),
                      (dict(_g3(must_not_open=True), set="negative", category="chit_chat"), "must_not_open")):
        with pytest.raises(G3Error, match=word):
            validate_g3(base + [bad])


def _run(*ops, **flags):
    """A case run: its drive operations as drive operation log entries (dry_run_drive.DriveOpLog)."""
    return dict(id="x", repeat=0, calls=[], ops=[{"op": o, "args": {}, "outcome": "ok", "stubbed": False} for o in ops],
                **flags)


def test_a_value_question_on_a_low_accuracy_card_must_open_the_file_and_a_find_must_not():
    search, opened = "search", "open"   # DriveOperations.search / .open
    assert hard_failures([_run(search, opened, must_open=True)]) == []
    assert any("not opened" in f for f in hard_failures([_run(search, must_open=True)]))
    assert hard_failures([_run(search, must_not_open=True)]) == []
    assert any("a file was opened" in f for f in hard_failures([_run(search, opened, must_not_open=True)]))
    assert hard_failures([_run(search)]) == []   # a case without the flags is not judged on opens


# -- known_hard in the v2 gold ------------------------------------------------------------------------------------------

def test_a_known_hard_case_is_reported_and_guards_nothing():
    from scripts.validation.drive_eval.v2_metrics import family_table

    hard = _case(id="hard", known_hard="a gap nobody decided yet")
    plain = _case(id="plain")
    rows = {"hard": {"tiers": {}}, "plain": {"tiers": {CLOSED: "about"}}}
    table = family_table([hard, plain], rows)
    assert table["families"]["exact"]["cases"] == 1 and table["families"]["exact"]["passed"] == 1   # only `plain`
    assert table["known_hard"] == {"hard": {"reason": "a gap nobody decided yet", "passed": False}}
    rows["hard"] = {"tiers": {CLOSED: "about"}}
    assert family_table([hard, plain], rows)["known_hard"]["hard"]["passed"] is True   # it shows when it starts to pass


def test_validate_refuses_an_empty_known_hard_reason():
    with pytest.raises(GoldError, match="known_hard"):
        validate_gold_v2([_case(known_hard="  ")], [CLOSED])


def test_the_unspaced_id_number_case_is_the_one_known_hard_case_of_the_gold():
    gold = json.loads((_DIR / "gold_v2.json").read_text(encoding="utf-8"))
    assert [c["id"] for c in gold if c.get("known_hard")] == ["ex-value-id-number"]
