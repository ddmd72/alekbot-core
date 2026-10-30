"""The policy 3 upgrade re-plans media metadata-only records to pending, with no download, no extractor call and no
model work (USER_DRIVE_INDEXING_RFC §12.3, §27.3, Δ96)."""
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain.drive_card import card_hash
from src.domain.drive_index import (
    ACCURACY_VERSION, CURRENT_VERSIONS, SEARCH_V2_VERSIONS, ContentClass, DriveIndexRecord, DriveSyncState, EpochPhase,
    Reason, RecordState, Scope,
)
from src.domain.drive_keys import name_keys_v2

from .drive_content_world import ContentWorld
from .drive_index_fakes import ROOT, T0

SPACE = "test-space/8"


@pytest.fixture
async def w():
    world = ContentWorld()
    world.audio = world.drive.add_file("memo.m4a", b"\x00" * 64, mime_type="audio/mp4")
    change = world.drive.items[world.audio]
    old_policy = {**{k: v for k, v in CURRENT_VERSIONS.items()}, "policy": 2}
    rec = DriveIndexRecord(
        user_id="u", connection_id="u", item_id=change.item_id, identity=change.identity, parent_id=change.parent_id, name=change.name,
        size_bytes=change.size_bytes, mime_type=change.mime_type, kind="audio", content_class=ContentClass.MEDIA,
        scope=Scope.NORMAL, state=RecordState.METADATA_ONLY, reason=Reason.KIND_NOT_ADMITTED,
        content_version=change.content_version, modified_at=change.modified_at, fs_modified_at=change.fs_modified_at,
        effective_date=change.fs_modified_at, policy_version=2, card_version=CURRENT_VERSIONS["card"],
        embedding_space=SPACE, has_card=True, card_rank=1, name_tokens=name_keys_v2(change.name),
        name_keys_version=CURRENT_VERSIONS["name_keys"], geo_table_version=CURRENT_VERSIONS["geo_table"],
        seen_epoch=1)
    rec = replace(rec, card_hash=card_hash(rec))
    await world.repo.upsert_records([rec], {rec.item_id: [0.5] * 8})
    world.repo.states["u"] = DriveSyncState(
        user_id="u", connection_id="u", drive_identity="fake:d1", root_item_id=ROOT, cursor=f"delta:{len(world.drive.log)}", round_complete=True,
        epoch=1, epoch_phase=EpochPhase.IDLE, initial_enumeration_done=True, created_at=T0,
        applied_versions=old_policy, applied_space=SPACE, observed_as_of=T0)
    world.clock.advance(timedelta(days=1).total_seconds())
    return world


async def test_upgrade_pass_reclassifies_media_metadata_only(w):
    await w.settle()
    rec = w.rec(w.audio)
    assert (rec.state, rec.reason, rec.policy_version) == (RecordState.PENDING, None, CURRENT_VERSIONS["policy"])
    assert rec.has_card and rec.card_hash == card_hash(rec)   # the same card: no re-embed
    assert w.emb.calls == []
    assert w.parser.requests == [] and w.reader.requests == [] and w.triage.calls == []
    assert w.drive.downloads == [] and w.drive.thumbnail_calls == []
