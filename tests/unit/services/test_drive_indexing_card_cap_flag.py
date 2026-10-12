"""The card cap is a flag beside the structural reason (USER_DRIVE_INDEXING_RFC §5.2, §5.4): a record without card
headroom keeps the scope and reason its structure gave it, so a capped repository stays opaque, re-opens when its
marker leaves, and a released dense folder is carded with its own label."""
from src.domain.drive_card import card_text
from src.domain.drive_index import Reason, Scope
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import World


async def test_a_capped_repository_stays_opaque_on_every_re_emission(monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 3)
    w = World(page_size=500)
    for i in range(3):
        w.drive.add(f"doc{i}.pdf")
    repo = w.drive.add("proj", folder=True)
    for f in range(5):
        w.drive.add(f"m{f}.py", parent=repo)
    w.drive.add(".git", parent=repo, folder=True)
    await w.settle()
    rec = w.rec(repo)
    assert rec.card_capped and not rec.has_card
    assert (rec.scope, rec.reason) == (Scope.OPAQUE_ROOT, Reason.OPAQUE_ROOT)
    for _ in range(3):
        w.drive.children_calls.clear()
        w.drive.reemit(repo)                     # Graph re-emits unchanged items often (S0a)
        await w.settle()
        assert w.drive.children_calls == []      # never re-opened, never re-listed
        rec = w.rec(repo)
        assert rec.scope is Scope.OPAQUE_ROOT and rec.reason is Reason.OPAQUE_ROOT and rec.card_capped


async def test_a_capped_repository_re_opens_when_its_marker_leaves(monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 1)
    w = World(page_size=500)
    w.drive.add("keep.pdf")
    repo = w.drive.add("proj", folder=True)
    main = w.drive.add("main.py", parent=repo)
    git = w.drive.add(".git", parent=repo, folder=True)
    await w.settle()
    assert w.rec(repo).card_capped and w.rec(repo).scope is Scope.OPAQUE_ROOT and w.rec(main) is None
    w.drive.delete(git)
    await w.settle()
    rec = w.rec(repo)
    assert rec.scope is Scope.NORMAL and rec.reason is Reason.FOLDER and rec.card_capped
    assert w.rec(main) is not None               # its never-stored subtree was re-listed


async def test_a_released_dense_folder_keeps_its_generated_files_label(monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 1)
    w = World(page_size=1000)
    w.drive.add("keep.pdf")
    dense = w.drive.add("logs", folder=True)
    for f in range(501):
        w.drive.add(f"x{f}.log", parent=dense)
    await w.settle()
    rec = w.rec(dense)
    assert rec.reason is Reason.DENSE_TREE and rec.card_capped and not rec.has_card
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 10)
    w.drive.reemit(dense)                        # any tick with headroom releases it
    await w.settle()
    rec = w.rec(dense)
    assert not rec.card_capped and rec.has_card and rec.reason is Reason.DENSE_TREE
    assert "generated files" in card_text(rec) and "repository" not in card_text(rec)


async def test_release_clears_the_flag_and_cards_best_rank_first(monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 1)
    w = World()
    doc = w.drive.add("a.pdf")
    code = w.drive.add("b.py")
    await w.settle()
    assert w.rec(doc).has_card and w.rec(code).card_capped
    assert w.rec(code).reason is Reason.KIND_NOT_ADMITTED          # the structural reason stays
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 5)
    w.drive.reemit(doc)
    await w.settle()
    assert w.carded() == {doc, code} and not w.rec(code).card_capped
