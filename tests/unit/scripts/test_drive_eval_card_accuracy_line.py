"""Eval additions (fix round): the `expect_accuracy` case rule (a scan's card carries `card accuracy: N%`, a photo's
none), the capture-code digest in the ParseCache key, the reader version mirror, and the gold edits."""
import json
from pathlib import Path

import pytest

from scripts.validation.drive_eval import parse_cache, search_v2_eval
from scripts.validation.drive_eval.parse_cache import ParseCache
from scripts.validation.drive_eval.v2_metrics import GoldError, case_pass, validate_gold_v2
from src.composition.drive_reader_runner import READER_VERSION
from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest, ExtractionStatus

from ..services.drive_content_fakes import FakeExtractor

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"
SCAN = "Documents/DOC_20260610_0042.pdf"
PHOTO = "Photos/Camera/IMG_20260814_204712.jpg"


def _case(**kw):
    return {"id": "c", "family": "class", "query": "q", "expected_paths": [SCAN], "expect_tier": "about", **kw}


def test_a_scan_card_must_carry_the_accuracy_line_and_a_photo_card_must_not():
    case = _case(expect_accuracy={SCAN: True, PHOTO: False})
    base = {"tiers": {SCAN: "about"}}
    assert case_pass(case, {**base, "accuracy": {SCAN: 88, PHOTO: None}})
    assert not case_pass(case, {**base, "accuracy": {SCAN: None}})              # the scan without its line
    assert not case_pass(case, {**base, "accuracy": {}})                         # the scan not shown as a card
    assert not case_pass(case, {**base, "accuracy": {SCAN: 88, PHOTO: 100}})    # a photo with a line
    assert case_pass(_case(), base)                                              # a plain case is unchanged


def test_validate_refuses_a_malformed_expect_accuracy():
    for bad in ({"expect_accuracy": True}, {"expect_accuracy": {}}, {"expect_accuracy": {SCAN: "yes"}},
                {"expect_accuracy": {"nope.pdf": True}}, {"family": "negative", "expected_paths": [],
                                                           "expect_accuracy": {SCAN: True}}):
        with pytest.raises(GoldError, match="expect_accuracy"):
            validate_gold_v2([_case(**bad)], [SCAN])


def test_the_gold_sets_the_scan_and_photo_accuracy_expectations_and_the_screenshot_alternatives():
    gold = {c["id"]: c for c in json.loads((_DIR / "gold_v2.json").read_text(encoding="utf-8"))}
    assert gold["cls-scans-en"]["expect_accuracy"] == {SCAN: True}
    assert gold["cls-sunsets-en"]["expect_accuracy"] == {PHOTO: False}
    v1_shots = {c for c in json.loads((_DIR / "gold_g2.json").read_text(encoding="utf-8")) for c in c["expected_paths"]
                if c.startswith("Screenshots/")}
    for cid in ("cls-screenshot-ru", "cls-screenshots-en"):
        assert v1_shots <= set(gold[cid]["acceptable_paths"]) and len(v1_shots) == 3


def test_the_g3_ru_value_case_asks_for_a_value_the_card_prints():
    g3 = {c["id"]: c for c in json.loads((_DIR / "gold_g3_v2.json").read_text(encoding="utf-8"))}
    case = g3["v2-card-value-opens-ru"]
    assert case["must_open"] is True and "выдано" in case["text"]
    assert any("2021" in w for w in case["answer_must_include"][0])


def test_the_reader_version_mirrors_the_runner():
    assert search_v2_eval.READER_VERSION == READER_VERSION


async def test_a_capture_code_change_misses_a_warm_parse_cache(tmp_path, monkeypatch):
    source = tmp_path / "a.pdf"
    source.write_bytes(b"%PDF")
    request = ExtractionRequest(path=str(source), name="a.pdf", kind="pdf", mime_type="application/pdf", user_id="u",
                                account_id="a")
    inner = FakeExtractor(ExtractionOutcome(status=ExtractionStatus.TEXT, text="x"))
    cache_dir = tmp_path / "cache"
    await ParseCache(inner, cache_dir).extract(request)
    warm = ParseCache(inner, cache_dir)
    await warm.extract(request)
    assert warm.hits == 1 and warm.misses == 0
    monkeypatch.setattr(parse_cache, "CAPTURE_DIGEST", "an edited capture.py")
    edited = ParseCache(inner, cache_dir)
    await edited.extract(request)
    assert edited.hits == 0 and edited.misses == 1
    assert len(parse_cache._capture_digest()) == 16
