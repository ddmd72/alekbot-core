"""The eval's special cases test what they claim (USER_DRIVE_INDEXING_RFC §17.8, §10.1 step 3a):
- the trigram-only name case shares no whole token with its target, only trigrams above the floor;
- an accent case reaches its target through an unaccented whole-token key;
- a semantic case is out of the name stream's reach: no key of the query matches its target's name;
- place cases accept min_hits right answers; the "photos" date variant is labelled known-hard."""
import json
from pathlib import Path

from src.domain.drive_name_keys import name_keys, name_match, query_name_keys

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"


def _gold():
    return {c["id"]: c for c in json.loads((_DIR / "gold_g1.json").read_text(encoding="utf-8"))}


def _name(path):
    return path.rsplit("/", 1)[-1]


def test_the_trigram_only_case_matches_by_trigrams_alone():
    case = _gold()["misspelled-trigram-only"]
    record = set(name_keys(_name(case["expected_paths"][0])))
    query = query_name_keys(case["query"])
    assert not [k for k in query if k.startswith("w:") and k in record]
    assert name_match(tuple(record), query) is not None


def test_accent_cases_reach_their_target_by_a_whole_token():
    gold = _gold()
    for cid in ("translit-accent-resume", "translit-accent-coeur", "translit-accent-ano"):
        case = gold[cid]
        record = set(name_keys(_name(case["expected_paths"][0])))
        assert case["query"].isascii()
        assert [k for k in query_name_keys(case["query"]) if k.startswith("w:") and k in record], cid


def test_semantic_cases_are_out_of_the_name_streams_reach():
    semantic = [c for c in _gold().values() if c["case"] == "semantic"]
    assert len(semantic) >= 3 and {c["lang"] for c in semantic} >= {"ru", "fr"}
    for case in semantic:
        for path in case["expected_paths"]:
            assert name_match(name_keys(_name(path)), query_name_keys(case["query"])) is None, case["id"]


def test_place_cases_accept_min_hits_and_the_photos_variant_is_known_hard():
    gold = _gold()
    assert all(c.get("min_hits") == 5 for c in gold.values() if c["case"] == "place")
    assert "date-rome-august" not in gold
    assert gold["date-rome-august-photos-word"]["known_hard"]
    assert gold["date-rome-august-photo-filter"]["query"] == "" and gold["date-rome-august-photo-filter"][
        "filters"]["kind"] == "photo"
    assert gold["date-rome-august-rome"]["query"] == "Rome"
