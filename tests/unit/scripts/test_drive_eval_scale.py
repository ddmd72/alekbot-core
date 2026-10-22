"""Scale corpora and the floor calibration procedure (USER_DRIVE_INDEXING_RFC §25.7; scale_corpus.py,
scale_calibrate.py): the corpus is deterministic and PII-free in shape, a near-miss shares no key with its words,
the procedure picks the smallest floor meeting the negatives' bar and judges it by the near-miss recall, and a small
corpus runs end to end through the real tick and search."""
import pytest
import random

from scripts.validation.drive_eval.scale_calibrate import calibrate, run_size
from scripts.validation.drive_eval.scale_corpus import HELD_OUT, TOPICS, make_corpus, misspell
from src.domain.drive_keys import query_concepts, word_form_keys


def test_the_corpus_is_deterministic_and_mixed():
    a, b = make_corpus(300, seed=3), make_corpus(300, seed=3)
    assert a.manifest == b.manifest and a.texts == b.texts and a.near_misses == b.near_misses
    photos = sum(1 for t in a.truth.values() if t["kind"] == "photo")
    assert 30 < photos < 100 and len(a.texts) == 300 - photos
    assert max(len(t) for t in a.texts.values()) > 6000      # long documents span many chunks
    assert make_corpus(300, seed=4).manifest != a.manifest


def test_held_out_topics_are_in_no_body():
    corpus = make_corpus(400)
    bodies = " ".join(corpus.texts.values()).casefold()
    assert not [w for w in HELD_OUT if w in bodies]


def test_a_misspelled_word_shares_no_key_with_the_original():
    rng = random.Random(1)
    for vocab, *_ in TOPICS.values():
        for word in vocab:
            if " " in word or "-" in word:
                continue
            wrong = misspell(word, rng)
            keys = set(query_concepts(wrong).concepts[0].word_keys) if query_concepts(wrong).concepts else set()
            assert not keys & set(word_form_keys(word)), (word, wrong)


def _q(meaning, matched=()):
    return {"meaning": meaning, "matched": set(matched)}


def test_calibrate_picks_the_smallest_floor_meeting_the_negatives_bar():
    negatives = [_q({"x1": 3.1, "x2": 2.6, "x3": 1.0}), _q({"y1": 2.2})]
    near = [(_q({"t1": 3.4, "x9": 0.5}), "topic"), (_q({"t2": 1.9}), "topic")]
    topic_of = {"t1": "topic", "t2": "topic"}
    out = calibrate(negatives, near, topic_of)
    assert out["floor"] == 2.25                              # 2.0 -> (2+1)/2 = 1.5 lines; 2.25 -> (2+0)/2 = 1.0
    assert out["near_recall_at_floor"] == 0.5 and out["accepted"] is False
    assert out["at_current_z_related"]["t"] == 2.5


def test_matched_files_never_count_as_related():
    out = calibrate([_q({"x1": 9.0}, matched=["x1"])], [], {})
    assert out["floor"] == 0.5


@pytest.mark.datafile
@pytest.mark.slow
async def test_a_small_corpus_runs_end_to_end():
    row = await run_size(60, seed=2)
    assert row["records"].get("carded", 0) >= 60 and row["negatives_lost_to_common"] == []
    assert row["search_ms_p95"] > 0 and row["reads_max"] > 0
    assert set(row["calibration"]) >= {"floor", "near_recall_at_floor", "accepted", "grid"}
