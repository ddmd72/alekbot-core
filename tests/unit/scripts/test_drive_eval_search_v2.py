"""The Search v2 eval harness end to end, offline (USER_DRIVE_INDEXING_RFC §25.7, F-22): a tiny drive is indexed once
through the recording ports (fake model runners), the snapshot is saved and loaded, and the eval re-indexes it from
the replays alone with lexical vectors, captures every tier member through the real DriveSearchService, and scores
the families. A snapshot that misses an input gives no verdict."""
import json
from datetime import date

import pytest

from scripts.validation.drive_eval import search_v2_eval
from scripts.validation.drive_eval.enrichment_record import (
    CostMeter, RecordingPassages, RecordingProfiler, RecordingReader, RecordingTriage,
)
from scripts.validation.drive_eval.enrichment_snapshot import EnrichmentSnapshot
from scripts.validation.drive_eval.lexical_embeddings import EMBEDDING_SPACE, LexicalEmbeddings, embed
from scripts.validation.drive_eval.runner import EvalClock, index_fixture
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_enrichment import DriveProfile, HintItem, PhotoDescription, ProfileDate, ProfileEntity
from src.domain.drive_enrichment_calls import HintReply, ModelReplyStatus, ProfileReply, TriageReply, TriageResultV2
from src.domain.drive_extraction import PARSER, READER
from src.domain.drive_index import TriageVerdict
from src.domain.place_lookup import Place

from ..services.drive_content_fakes import FakeExtractor

S = ModelReplyStatus
ROME_NOTE = ("We spent a week in Rome visiting the Colosseum and walking through Trastevere every evening. "
             "The trattoria near the river served the best carbonara of the trip, and the museum queues were short "
             "in the morning. Next time: the Vatican gardens and a day trip to the coast.\n")
LEASE_NOTE = ("The lease for the flat in Porto runs twelve months from the first of January. Notice to end it is "
              "sixty days in writing, the deposit is two months of rent, and the landlord pays for the boiler "
              "service once a year. Keep the signed copy with the insurance papers.\n")
PROFILES = {
    "rome.md": DriveProfile(title="Rome trip notes", what_it_is="Notes from a week in Rome", language="en",
                            doc_type="notes", life_domains=("travel",),
                            entities=(ProfileEntity(name="Rome", type="place", printed="Rome", forms=("Рим",)),),
                            topic_words=("travel", "viaje")),
    "lease.md": DriveProfile(title="Porto lease notes", what_it_is="Terms of a flat lease", language="en",
                             doc_type="notes", life_domains=("home",),
                             dates=(ProfileDate(label="valid_from", iso=date(2026, 1, 1)),),
                             label_words=("lease", "contrato de alquiler", "договор аренды")),
}


class Profiler:
    def __init__(self):
        self.calls = 0

    async def profile(self, request):
        self.calls += 1
        return ProfileReply(status=S.OK, profile=PROFILES[request.name], model="fake", usd=0.0008)


class Passages:
    async def hints(self, request):
        items = {i: (HintItem(text="Italy" if "Rome" in t else "rental", type="keyword",
                              evidence=" ".join(t.split()[:4])),) for i, t in request.chunks}
        return HintReply(status=S.OK, items_by_index=items, model="fake", usd=0.0004,
                         outcome_by_index={i: S.OK for i, _ in request.chunks})


class Triage:
    async def triage(self, user_id, account_id, images):
        return TriageReply(status=S.OK, model="fake", usd=0.0001, results=tuple(
            TriageResultV2(item_id=i.item_id, verdict=TriageVerdict.PHOTO,
                           photo=PhotoDescription(description="a fountain in a square at night")) for i in images))


def _tree(tmp_path):
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "rome.md").write_text(ROME_NOTE, encoding="utf-8")
    (tmp_path / "files" / "lease.md").write_text(LEASE_NOTE, encoding="utf-8")
    (tmp_path / "files" / "pic.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    tree = {"items": [
        {"path": "Notes/rome.md", "mime": "text/markdown", "fs_modified": "2025-09-01T10:00:00Z",
         "content": "files/rome.md"},
        {"path": "Notes/lease.md", "mime": "text/markdown", "fs_modified": "2026-01-02T10:00:00Z",
         "content": "files/lease.md"},
    ]}
    overlay = {"items": [
        {"path": "Photos/IMG_1.HEIC", "mime": "image/heic", "size": 3000000, "fs_modified": "2025-08-20T10:00:00Z",
         "facets": {"photo": {"takenDateTime": "2025-08-14T18:00:00Z", "cameraMake": "Apple"},
                    "location": {"latitude": 41.9, "longitude": 12.49}}, "thumbnail": "files/pic.jpg"}]}
    (tmp_path / "tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (tmp_path / "tree_v2.json").write_text(json.dumps(overlay), encoding="utf-8")
    return tmp_path / "tree.json", tmp_path / "tree_v2.json"


def rome(lat, lon):
    return Place(name="Rome (Roma), Italy", forms=("Rome", "Roma", "Рим", "Italy", "Italia"))


GOLD = [
    {"id": "ex-colosseum", "family": "exact", "sub": "content_only", "lang": "en", "query": "Colosseum",
     "filters": {}, "expected_paths": ["Notes/rome.md"], "expect_tier": "mentioned"},
    {"id": "sem-rental", "family": "semantic", "sub": "translation", "lang": "en", "query": "rental",
     "filters": {}, "expected_paths": ["Notes/lease.md"], "expect_tier": "translated"},
    {"id": "inc-rome", "family": "incident", "sub": "everything", "lang": "en", "query": "everything about Rome",
     "filters": {}, "expected_paths": ["Notes/rome.md", "Photos/IMG_1.HEIC"], "expect_tier": "any"},
    {"id": "neg-vaccination", "family": "negative", "sub": "absent", "lang": "en", "query": "vaccination",
     "filters": {}, "expected_paths": [], "expect_tier": "none", "expect_absent": ["vaccination"]},
]
BARS = {"pass_rate": {"exact": 0.95, "semantic": 0.85, "incident": 1.0, "negative": 0.9},
        "file_recall": {}, "exact_junk_per_case_max": 0.3, "automatic_junk_per_case_max": 0.5,
        "related_per_negative_max": 1.0, "book_in_exact_max": 0, "chars_median_max": 3000, "chars_max": 16000}


async def _record(tmp_path, tree, overlay):
    drive = search_v2_eval.v2_drive(tree, overlay)
    snap, meter = EnrichmentSnapshot(), CostMeter(cap_usd=1.0)
    parser = FakeExtractor(PARSER, version=2)
    await index_fixture(drive, InMemoryDriveIndexRepository(), LexicalEmbeddings(), rome, EMBEDDING_SPACE,
                        EvalClock(), throttle_pause_s=0,
                        extractors={PARSER: parser, READER: RecordingReader(FakeExtractor(READER), snap, meter)},
                        triage=RecordingTriage(Triage(), snap, meter, path_of=drive.path_of),
                        profiler=RecordingProfiler(Profiler(), snap, meter, path_of=drive.path_of),
                        passages=RecordingPassages(Passages(), snap, meter, path_of=drive.path_of))
    path = tmp_path / "enrichment.json"
    snap.save(path)
    return path, meter


async def _evaluate(tmp_path, snapshot_path, tree, overlay, **kw):
    (tmp_path / "gold_v2.json").write_text(json.dumps(GOLD), encoding="utf-8")
    (tmp_path / "bars_v2.json").write_text(json.dumps(BARS), encoding="utf-8")
    return await search_v2_eval.evaluate(
        tree=tree, overlay=overlay, gold_v2_path=tmp_path / "gold_v2.json", gold_v1_paths=[],
        bars_path=tmp_path / "bars_v2.json", snapshot_path=snapshot_path,
        embeddings=search_v2_eval.build_embeddings("lexical", EnrichmentSnapshot.load(snapshot_path))[0],
        space=EMBEDDING_SPACE, place_of=rome, parser=FakeExtractor(PARSER, version=2), **kw)


async def test_record_then_replay_scores_every_family_with_no_model_call(tmp_path):
    tree, overlay = _tree(tmp_path)
    snapshot_path, meter = await _record(tmp_path, tree, overlay)
    snap = EnrichmentSnapshot.load(snapshot_path)
    assert len(snap.profiles) == 2 and len(snap.photos) == 1 and snap.hints and meter.spent > 0
    report = await _evaluate(tmp_path, snapshot_path, tree, overlay)
    assert report["index"]["misses"] == {}
    rows = report["rows"]
    assert rows["ex-colosseum"]["tiers"]["Notes/rome.md"] == "mentioned"
    assert rows["sem-rental"]["tiers"]["Notes/lease.md"] == "translated"   # a grounded passage keyword
    assert rows["inc-rome"]["tiers"]["Photos/IMG_1.HEIC"] == "about"
    assert rows["neg-vaccination"]["absent"] == ["vaccination"]
    assert not any(t in ("about", "mentioned", "translated", "labelled")
                   for t in rows["neg-vaccination"]["tiers"].values())
    fams = report["families"]["families"]
    assert fams["exact"]["passed"] == 1 and fams["negative"]["passed"] == 1 and fams["incident"]["passed"] == 1
    assert report["verdict"] in ("pass", "bars_missed")


async def test_a_snapshot_that_misses_an_input_gives_no_verdict(tmp_path):
    tree, overlay = _tree(tmp_path)
    empty = tmp_path / "empty.json"
    EnrichmentSnapshot().save(empty)
    report = await _evaluate(tmp_path, empty, tree, overlay)
    assert report["verdict"] == "stale_snapshot" and report["index"]["misses"]["profile"] == 2


async def test_misses_may_be_allowed_for_a_partial_run(tmp_path):
    tree, overlay = _tree(tmp_path)
    empty = tmp_path / "empty.json"
    EnrichmentSnapshot().save(empty)
    report = await _evaluate(tmp_path, empty, tree, overlay, allow_misses=True)
    assert report["misses_allowed"] and "families" in report


def test_the_overlay_adds_items_to_the_tree(tmp_path):
    tree, overlay = _tree(tmp_path)
    paths = search_v2_eval.v2_drive(tree, overlay).paths()
    assert "Photos/IMG_1.HEIC" in paths and "Notes/rome.md" in paths
    assert search_v2_eval.v2_drive(tree, None).paths().count("Photos/IMG_1.HEIC") == 0


def test_lexical_vectors_are_deterministic_unit_and_lexical():
    a, b, c = embed("lease agreement Porto"), embed("Porto lease"), embed("cholesterol results")
    assert a == embed("lease agreement Porto")
    assert sum(x * x for x in a) == pytest.approx(1.0)
    cos = lambda x, y: sum(p * q for p, q in zip(x, y))   # noqa: E731
    assert cos(a, b) > cos(a, c)


async def test_a_recorded_keyword_set_is_embedded_as_its_passage_but_a_query_never_is():
    from scripts.validation.drive_eval.lexical_embeddings import AnchoredLexicalEmbeddings
    emb = AnchoredLexicalEmbeddings({"Italy": ["no such quote", "We spent a week"], "travel": ["in Rome visiting"]})
    chunk = await emb.get_embedding(ROME_NOTE)
    assert await emb.get_embedding("Italy\ntravel") == chunk and emb.anchored == 1
    assert await emb.get_embedding("Italy", task_type="RETRIEVAL_QUERY") == embed("Italy")
    assert await emb.get_embedding("Italy\nunknown") == embed("Italy\nunknown")
