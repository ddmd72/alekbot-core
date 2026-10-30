"""A name-keys version bump re-keys every record through the upgrade pass and embeds no card
(USER_DRIVE_INDEXING_RFC §12.3)."""
from src.domain import drive_index_planner as planner
from src.domain.drive_index import CURRENT_VERSIONS
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import EXTRACTOR_VERSIONS, World
from .drive_index_fakes import U_SCOPE


async def test_a_keyer_bump_re_keys_every_record_without_re_embedding(monkeypatch):
    w = World()
    names = [w.drive.add(f"doc{i}.pdf") for i in range(5)] + [w.drive.add("Photos", folder=True)]
    await w.settle()
    target = {**CURRENT_VERSIONS, "name_keys": CURRENT_VERSIONS["name_keys"] + 1}
    monkeypatch.setattr(planner, "NAME_KEYS_VERSION", target["name_keys"])
    monkeypatch.setattr(planner, "name_keys_v2", lambda name: (f"v2:{name}",))   # the planner's keyer (§25.2.4)
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", target)
    w.emb.calls.clear()
    await w.settle()
    assert dict((await w.repo.get_sync_state(U_SCOPE)).applied_versions) == {**target, **EXTRACTOR_VERSIONS}
    for item_id in names:
        rec = w.rec(item_id)
        assert rec.name_tokens == (f"v2:{rec.name}",) and rec.name_keys_version == target["name_keys"]
    assert w.emb.calls == []
