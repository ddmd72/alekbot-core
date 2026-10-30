"""A version flip on a live index (USER_DRIVE_INDEXING_RFC §24.1, §12.3, §7.8): the upgrade pass makes every record
built under older versions due in class 3, the work then runs, and the versions apply only when a pass finds nothing
stale. Search v2 (D30): an index v1 built (no `keys` stamp) is never upgraded — that gate is
test_drive_indexing_enrichment_tick.test_a_v1_index_does_no_tick_work_until_its_rebuild — so the live index here is a
v2 one (its Search v2 keys stamped) whose policy, card, geo, name-key, extraction and triage versions predate a bump,
which is the upgrade pass's remaining job (§25.8: v2 to v2.x)."""
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain.drive_index import (
    ACCURACY_VERSION, CURRENT_VERSIONS, SEARCH_V2_VERSIONS, ContentClass, DriveIndexRecord, DriveSyncState, EpochPhase, Priority, Reason, RecordState,
    Scope, TriageVerdict,
)
from src.domain.drive_index_planner import content_due, triage_due
from src.domain.drive_keys import name_keys_v2

from .drive_content_world import ContentWorld, prose
from .drive_index_fakes import ROOT, T0
from .drive_index_fakes import U_SCOPE

SLICE1_VERSIONS = {"policy": 1, "card": 1, "geo_table": 1, "name_keys": 1}
# the state's stamps: the older versions above, with the Search v2 keys and `accuracy` (the migration gate key since
# Δ65) current
APPLIED_VERSIONS = {**SLICE1_VERSIONS, **SEARCH_V2_VERSIONS, "accuracy": ACCURACY_VERSION}
SPACE = "test-space/8"


def slice1_record(change, kind, cclass, rank, *, folder=False):
    """A record exactly as Slice 1 left it: metadata only, carded under card version 1."""
    return DriveIndexRecord(
        user_id="u", item_id=change.item_id, parent_id=change.parent_id, name=change.name, is_folder=folder,
        size_bytes=change.size_bytes, mime_type=change.mime_type, facets=dict(change.facets), kind=kind,
        content_class=cclass, scope=Scope.NORMAL, state=RecordState.METADATA_ONLY,
        reason=Reason.FOLDER if folder else Reason.KIND_NOT_ADMITTED, content_version=change.content_version,
        modified_at=change.modified_at, fs_modified_at=change.fs_modified_at, effective_date=change.fs_modified_at,
        policy_version=1, card_version=1, embedding_space=SPACE, card_hash="slice1-hash", has_card=True,
        card_rank=rank, name_tokens=name_keys_v2(change.name), name_keys_version=1, seen_epoch=1)


@pytest.fixture
async def w():
    world = ContentWorld()
    folder = world.drive.add("Contracts", folder=True)
    world.pdf = world.add_doc("Договор.pdf", prose(40), parent=folder)
    world.img = world.add_image("IMG_1.jpg", parent=folder)
    world.code = world.drive.add_file("main.py", b"print(1)", parent=folder)
    world.folder = folder
    world.repo_root = world.drive.add("alek-site", folder=True)
    world.git = world.drive.add(".git", parent=world.repo_root, folder=True)
    world.drive.add_file("README.md", b"Family recipes as a static site.", parent=world.repo_root)
    items = world.drive.items
    records = [slice1_record(items[folder], "folder", ContentClass.FOLDER, 3, folder=True),
               slice1_record(items[world.pdf], "pdf", ContentClass.DOCUMENT, 0),
               slice1_record(items[world.img], "image", ContentClass.MEDIA, 1),
               slice1_record(items[world.code], "code", ContentClass.OTHER, 2),
               replace(slice1_record(items[world.repo_root], "folder", ContentClass.FOLDER, 3, folder=True),
                       scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)]
    git = replace(slice1_record(items[world.git], "folder", ContentClass.FOLDER, 3, folder=True),
                  scope=Scope.INSIDE_OPAQUE, has_card=False, card_hash=None, card_version=0, embedding_space="")
    await world.repo.upsert_records([git])
    await world.repo.upsert_records(records, {r.item_id: [0.5] * 8 for r in records})
    world.repo.states["u"] = DriveSyncState(
        user_id="u", drive_id="d1", root_item_id=ROOT, cursor=f"delta:{len(world.drive.log)}", round_complete=True,
        epoch=1, epoch_phase=EpochPhase.IDLE, initial_enumeration_done=True, created_at=T0,
        applied_versions=APPLIED_VERSIONS, applied_space=SPACE, observed_as_of=T0)
    world.clock.advance(timedelta(days=1).total_seconds())
    return world


async def test_the_upgrade_pass_makes_slice1_records_due_in_class_3(w):
    assert dict(CURRENT_VERSIONS) != APPLIED_VERSIONS   # the flip is in
    await w.tick()
    for item in (w.folder, w.pdf, w.img, w.code):
        rec = w.rec(item)
        assert (rec.priority, rec.policy_version) == (Priority.MAINTENANCE, CURRENT_VERSIONS["policy"]), rec.name
        assert rec.due_at <= w.clock.now
    assert content_due(w.rec(w.pdf)) and triage_due(w.rec(w.img))
    assert w.rec(w.code).reason is Reason.KIND_NOT_ADMITTED
    assert w.parser.requests == [] and w.triage.calls == []   # fresh work first: the pass only schedules


async def test_the_scheduled_work_runs_and_the_versions_apply(w):
    await w.settle()
    pdf, img, code = w.rec(w.pdf), w.rec(w.img), w.rec(w.code)
    assert pdf.state is RecordState.INDEXED and w.readable(w.pdf)
    assert (img.reason, img.triage_verdict) == (Reason.PHOTOGRAPH, TriageVerdict.PHOTO)
    for rec in (pdf, img, code, w.rec(w.folder)):
        assert rec.card_version == CURRENT_VERSIONS["card"] and rec.has_card and rec.due_at is None
    state = await w.repo.get_sync_state(U_SCOPE)
    assert dict(state.applied_versions) == {**CURRENT_VERSIONS, "extractor:parser": 1, "extractor:reader": 1}
    assert state.applied_space == SPACE


async def test_an_upgraded_repository_reads_its_readme_for_its_profile(w):
    # v2 (§25.3, §25.2.7): the README becomes the folder's `readme_excerpt` and its profile job is ready (the
    # profile, not lead text, then carries it into the card; that run is test_a_hidden_folder_is_profiled_from_its_readme)
    await w.settle()
    root = w.rec(w.repo_root)
    assert (root.scope, root.reason) == (Scope.OPAQUE_ROOT, Reason.OPAQUE_ROOT)
    assert root.readme_excerpt.startswith("Family recipes as a static site.")
    assert root.readme_excerpt_version and root.profile_state == "ready" and root.enrich_ready


async def test_upgrade_work_keeps_class_3_after_its_run(w, monkeypatch):
    """A record the pass scheduled is carded after its run in class 3, never jumping ahead of edits (§7.8)."""
    seen = []
    real = w.svc._embed_cards

    async def watch(user_id, recs, now, summary):
        seen.extend((r.item_id, r.priority) for r in recs)
        await real(user_id, recs, now, summary)
    monkeypatch.setattr(w.svc, "_embed_cards", watch)
    await w.settle()
    assert (w.pdf, Priority.MAINTENANCE) in seen and (w.img, Priority.MAINTENANCE) in seen
    assert not any(p == Priority.EDIT for item, p in seen if item in (w.pdf, w.img))
