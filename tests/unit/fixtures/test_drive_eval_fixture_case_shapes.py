"""The eval's special cases test what they claim (USER_DRIVE_INDEXING_RFC §17.8, §10.1 step 3a):
- the trigram-only name case shares no whole token with its target, only trigrams above the floor;
- an accent case reaches its target through an unaccented whole-token key;
- a semantic case is out of the name stream's reach: no key of the query matches its target's name;
- a place case calls as the manifest says (the place in drive_query, drive_kind=photo, no min_hits); its
  type-word twin is known-hard and accepts min_hits; the "photos" date variant is labelled known-hard."""
import json
from pathlib import Path

from src.domain.drive_keys import name_keys_v2, name_match_v2, name_word_concepts, query_concepts
from src.domain.drive_tier_pool import concept_trigrams
from src.domain.drive_tiers import name_query_keys

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"


def _gold():
    return {c["id"]: c for c in json.loads((_DIR / "gold_g1.json").read_text(encoding="utf-8"))}


def _name(path):
    return path.rsplit("/", 1)[-1]


def query_name_keys(query):
    """The name keys the search queries with (v2: the v1 `query_name_keys` is retired, xc m3/m4)."""
    concepts = query_concepts(query).concepts
    if not concepts:           # xc F2: a query of ignored words only is searched by name, whole keys alone
        return [k for c in name_word_concepts(query) for k in name_query_keys(c)]
    return [k for c in concepts for k in (*name_query_keys(c), *concept_trigrams(c))]


def test_the_trigram_only_case_matches_by_trigrams_alone():
    case = _gold()["misspelled-trigram-only"]
    record = set(name_keys_v2(_name(case["expected_paths"][0])))
    query = query_name_keys(case["query"])
    assert not [k for k in query if not k.startswith("t:") and k in record]    # no whole key of any family
    assert name_match_v2(tuple(record), query) is not None


def test_accent_cases_reach_their_target_by_a_whole_token():
    gold = _gold()
    for cid in ("translit-accent-resume", "translit-accent-coeur", "translit-accent-ano"):
        case = gold[cid]
        record = set(name_keys_v2(_name(case["expected_paths"][0])))
        assert case["query"].isascii()
        assert [k for k in query_name_keys(case["query"]) if k.startswith("w:") and k in record], cid


def test_semantic_cases_are_out_of_the_name_streams_reach():
    semantic = [c for c in _gold().values() if c["case"] == "semantic"]
    assert len(semantic) >= 3 and {c["lang"] for c in semantic} >= {"ru", "fr"}
    for case in semantic:
        for path in case["expected_paths"]:
            assert name_match_v2(name_keys_v2(_name(path)), query_name_keys(case["query"])) is None, case["id"]


def test_place_cases_call_as_the_manifest_says_and_their_type_word_twins_are_known_hard():
    """A place goes in drive_query and the type in drive_kind (agent_manifest drive_query); the old
    type-word shape stays as a known-hard `*-type-word` twin with the same answers."""
    gold = _gold()
    type_words = {"photo", "photos", "фото", "фотографии"}
    place = {cid: c for cid, c in gold.items() if c["case"] == "place"}
    calls = {cid: c for cid, c in place.items() if not cid.endswith("-type-word")}
    twins = {cid: c for cid, c in place.items() if cid.endswith("-type-word")}
    assert len(calls) >= 4 and set(twins) == {f"{cid}-type-word" for cid in calls}
    for cid, case in calls.items():
        assert case["filters"] == {"kind": "photo"}, cid
        assert "min_hits" not in case and "known_hard" not in case, cid
        assert not set(case["query"].casefold().split()) & type_words, cid
        twin = twins[f"{cid}-type-word"]
        assert twin["filters"] == {} and twin["min_hits"] == 5 and twin["known_hard"].strip(), cid
        assert twin["expected_paths"] == case["expected_paths"] and twin["lang"] == case["lang"], cid
        assert set(twin["query"].casefold().split()) & type_words, cid
    assert "date-rome-august" not in gold
    assert gold["date-rome-august-photos-word"]["known_hard"]
    assert gold["date-rome-august-photo-filter"]["query"] == "" and gold["date-rome-august-photo-filter"][
        "filters"]["kind"] == "photo"
    assert gold["date-rome-august-rome"]["query"] == "Rome"


def test_semantic_queries_hold_no_type_words():
    """drive_query takes name words or a place or subject, never a type word (that is drive_kind)."""
    type_words = {"file", "files", "document", "documents", "photo", "photos", "файлы", "файл", "документы", "фото",
                  "fichier", "fichiers", "documentos", "archivos"}
    for case in _gold().values():
        if case["case"] == "semantic":
            assert not set(case["query"].casefold().split()) & type_words, case["id"]
