"""Card accuracy in the indexing service (USER_DRIVE_INDEXING_RFC §26.4 D35/Δ59-Δ61, §26.7 Δ65, §26.8 S3): the score
written wherever a write changes the profile, brief, capture or reading facts; reading facts from the extraction
outcome; the accuracy upgrade re-grading from stored text without a download or a model call; and the migration gate
on the `accuracy` key."""
from dataclasses import replace

import pytest

from src.domain import drive_accuracy, drive_index as di
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import ACCURACY_VERSION, MIGRATION_GATE_KEY
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_enrich_world import EnrichWorld

pytestmark = pytest.mark.usefixtures("admitted")


def doc_text(word: str = "Acme", n: int = 30) -> str:
    return f"{word} Rentals. " + prose(n)


@pytest.fixture
def w():
    return EnrichWorld()


def _bump(monkeypatch, version):
    monkeypatch.setattr(drive_accuracy, "ACCURACY_VERSION", version)
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {**di.CURRENT_VERSIONS, "accuracy": version})


# -- the score on every write that changes what it reads ----------------------------------------------------------

async def test_a_profiled_file_carries_its_score_and_the_version(w):
    doc = w.add_doc("lease.md", doc_text())
    await w.settle()
    rec = w.rec(doc)
    assert rec.profile is not None and rec.profile.graded            # kept, with a grade each (Δ59)
    assert (rec.card_accuracy, rec.accuracy_version) == (100, ACCURACY_VERSION)   # everything is in the file's text


async def test_a_value_the_text_does_not_hold_stays_on_the_card_and_lowers_the_score(w):
    doc = w.add_doc("lease.md", doc_text())
    from src.domain.drive_enrichment import DriveProfile, ProfileEntity
    w.profiler.profiles[doc] = DriveProfile(
        title="About Acme", what_it_is="A lease", language="en", doc_type="contract", life_domains=("home",),
        entities=(ProfileEntity(name="Acme", type="organization", printed="Acme"),
                  ProfileEntity(name="Ghost Corp", type="organization", printed="Ghost Corp")),
        topics=("lease",), topic_words=("lease",), label_words=("contract",))
    await w.settle()
    rec = w.rec(doc)
    assert {e.name for e in rec.profile.entities} == {"Acme", "Ghost Corp"}   # nothing dropped
    assert 0 < rec.card_accuracy < 100
    assert w.summary_total("graded_not_in_text_entity") == 1
    assert w.summary_total("verify_dropped_entities") == 0


async def test_reading_facts_come_from_the_extraction_outcome_and_are_priced(w):
    w.parser.outcomes["lease.md"] = ExtractionOutcome(
        status=ExtractionStatus.TEXT, text=doc_text(), pages_refused=2, model_fallback_cost=0.01, extracted_by="parser@1")
    doc = w.add_doc("lease.md", "ignored")
    await w.settle()
    rec = w.rec(doc)
    assert (rec.pages_refused, rec.read_by_fallback) == (2, True)
    assert rec.card_accuracy == 100 - 5 - 3                       # R: pages refused 5, the fallback model 3


async def test_the_score_is_not_in_the_card_text(w):
    doc = w.add_doc("lease.md", doc_text())
    await w.settle()
    assert "accuracy" not in w.card_texts()[doc]


# -- the accuracy upgrade ----------------------------------------------------------------------------------------

async def test_an_accuracy_bump_re_grades_from_stored_text_without_a_download_or_a_model_call(w, monkeypatch):
    docs = [w.add_doc(f"d{i}.md", doc_text(f"Firm{i}")) for i in range(3)]
    await w.settle()
    assert all(w.rec(d).accuracy_version == ACCURACY_VERSION for d in docs)
    downloads = len(w.drive.downloads)
    calls = (len(w.parser.requests), len(w.reader.requests), len(w.profiler.requests), len(w.passages.requests),
             len(w.emb.calls), len(w.triage.calls))
    graded = []
    real = svc_mod.grade
    monkeypatch.setattr(svc_mod, "grade", lambda *a, **k: graded.append(a[2]) or real(*a, **k))
    _bump(monkeypatch, ACCURACY_VERSION + 1)
    await w.settle()
    assert all(w.rec(d).accuracy_version == ACCURACY_VERSION + 1 for d in docs)
    assert all(w.rec(d).card_accuracy == 100 for d in docs)
    assert len(graded) == 3                                     # the stored profile, graded against the stored text
    assert len(w.drive.downloads) == downloads
    assert calls == (len(w.parser.requests), len(w.reader.requests), len(w.profiler.requests),
                     len(w.passages.requests), len(w.emb.calls), len(w.triage.calls))
    assert w.repo.states["u"].applied_versions["accuracy"] == ACCURACY_VERSION + 1


async def test_an_accuracy_bump_re_scores_with_the_new_weights_and_keeps_the_card_text(w, monkeypatch):
    doc = w.add_doc("lease.md", doc_text())
    await w.settle()
    text = w.card_texts()[doc]
    generation = w.rec(doc).indexed_generation
    monkeypatch.setattr(drive_accuracy, "READING_POINTS", {**drive_accuracy.READING_POINTS, "truncated": 40})
    w.repo.records[("u", doc)] = replace(w.rec(doc), truncated=True)
    _bump(monkeypatch, ACCURACY_VERSION + 1)
    await w.settle()
    assert w.rec(doc).card_accuracy == 60
    assert w.card_texts()[doc] == text and w.rec(doc).indexed_generation == generation   # no re-embed, no new generation


async def test_the_upgrade_re_grades_a_stored_profile_whose_value_the_text_lacks(w, monkeypatch):
    doc = w.add_doc("lease.md", doc_text())
    await w.settle()
    # the stored text changes under the profile (a stand-in for a grader that now reads it differently)
    key = next(k for k in w.repo.chunks if k[1] == doc)
    chunk, vec = w.repo.chunks[key]
    w.repo.chunks[key] = (replace(chunk, text=chunk.text.replace("Acme", "Zzzz")), vec)
    _bump(monkeypatch, ACCURACY_VERSION + 1)
    await w.settle()
    rec = w.rec(doc)
    assert any(g.value and g.grade == "not_in_text" for g in rec.profile.graded)
    assert rec.card_accuracy < 100


async def test_a_record_that_carries_no_score_never_holds_the_accuracy_key(w, monkeypatch):
    folder = w.drive.add("Contracts", folder=True)
    photo = w.add_image("IMG_1.jpg", parent=folder)
    await w.settle()
    assert w.rec(folder).card_accuracy is None and w.rec(photo).card_accuracy is None
    _bump(monkeypatch, ACCURACY_VERSION + 1)
    await w.settle()
    assert w.repo.states["u"].applied_versions["accuracy"] == ACCURACY_VERSION + 1
    assert w.rec(folder).card_accuracy is None and w.rec(photo).card_accuracy is None


# -- the migration gate (Δ65) -------------------------------------------------------------------------------------

def test_the_gate_key_is_accuracy_and_in_every_version_map():
    assert MIGRATION_GATE_KEY == "accuracy" and "accuracy" in di.CURRENT_VERSIONS


async def test_a_fresh_onboarding_stamps_every_current_version(w):
    w.add_doc("a.md", doc_text())
    await w.settle()
    applied = w.repo.states["u"].applied_versions
    assert {k: applied[k] for k in di.CURRENT_VERSIONS} == dict(di.CURRENT_VERSIONS)


async def test_an_index_without_the_accuracy_stamp_does_no_tick_work_until_its_rebuild(w):
    doc = w.add_doc("a.md", doc_text())
    await w.settle()
    state = w.repo.states["u"]
    w.repo.states["u"] = replace(state, applied_versions={k: v for k, v in state.applied_versions.items()
                                                          if k != "accuracy"})   # a v2.0 index, keys and all
    records = dict(w.repo.records)
    w.add_doc("b.md", doc_text("Brio"))
    queued = len(w.queue.tasks)
    result = await w.tick()
    assert result.status == "awaiting_migration"
    assert w.repo.records == records and len(w.queue.tasks) == queued
    w.users.enabled["u"] = False   # the rebuild: a purge, then an onboarding
    await w.settle()
    assert w.rec(doc) is None
    w.users.enabled["u"] = True
    await w.settle()
    assert MIGRATION_GATE_KEY in w.repo.states["u"].applied_versions
