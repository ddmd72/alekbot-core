"""Policy and scope over a hostile tree (USER_DRIVE_INDEXING_RFC §17.2, §5.2, §5.5; plan Δ16).

The real tick runs over the eval fixture (tests/fixtures/drive_eval/tree.json) through the eval's FixtureDrive,
imported as a namespace package like tests/unit/validation/test_price_consensus.py. Every listed path has the
(scope, reason) the RFC prescribes; a path mapped to None must have no record at all. Content-stage classes
(machine text, bombs, encrypted files) are Slice 2 and are not in this table.
"""
from pathlib import Path

import pytest

from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from scripts.validation.drive_eval.runner import EVAL_USER, EvalClock, index_fixture
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import Reason, Scope

from .drive_index_fakes import FakeEmbeddings

_TREE = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval" / "tree.json"
N, OR, IO = Scope.NORMAL, Scope.OPAQUE_ROOT, Scope.INSIDE_OPAQUE

EXPECTED = {
    # a repository: one card for the root; its folders stay lightweight; its files get no record (§5.2)
    "Projects/alek-site": (OR, Reason.OPAQUE_ROOT),
    "Projects/alek-site/.git": (IO, Reason.FOLDER),
    "Projects/alek-site/node_modules": (IO, Reason.FOLDER),
    "Projects/alek-site/src/module_00.py": None,
    "Projects/alek-site/package.json": None,
    "Projects/alek-site/.git/HEAD": None,
    # a venv without a dot-name: pyvenv.cfg is the marker, kept without a card so its removal re-opens the folder
    "Projects/venv-tools": (OR, Reason.OPAQUE_ROOT),
    "Projects/venv-tools/pyvenv.cfg": (IO, Reason.KIND_NOT_ADMITTED),
    "Projects/venv-tools/lib/python3.13/site-packages/requests/mod_00.py": None,
    # an extracted repository without .git: only the tool-made folders collapse (Δ16)
    "Projects/site-export": (N, Reason.FOLDER),
    "Projects/site-export/src/app.js": (N, Reason.KIND_NOT_ADMITTED),
    "Projects/site-export/node_modules": (OR, Reason.OPAQUE_ROOT),
    "Projects/site-export/node_modules/react": (IO, Reason.FOLDER),
    "Projects/site-export/node_modules/react/index.js": None,
    "Notes/scripts/__pycache__": (OR, Reason.OPAQUE_ROOT),
    "Notes/scripts/__pycache__/util.cpython-313.pyc": None,
    "Notes/scripts/util.py": (N, Reason.KIND_NOT_ADMITTED),
    # dot-folders and dot-files
    ".obsidian": (OR, Reason.DOT_NAME),
    ".obsidian/workspace.json": None,
    ".DS_Store": (N, Reason.DOT_NAME),
    # density over content class, decided from a probe (§5.5 class 1)
    "Dumps/site-mirror": (OR, Reason.DENSE_TREE),
    "Dumps/site-mirror/page-000.bin": None,
    "Vault": (N, Reason.FOLDER),                       # 800 small notes: documents, never dense
    "Vault/note-799.md": (N, Reason.KIND_NOT_ADMITTED),
    "Archive/Invoices 2024": (N, Reason.FOLDER),       # 600 PDFs
    "Archive/Camera roll": (N, Reason.FOLDER),         # 1 000 photos: media, never dense
    "Archive/Camera roll/IMG_0417.HEIC": (N, Reason.KIND_NOT_ADMITTED),
    # never-download names (§5.1): they keep a card with their reason
    "Keys/id_rsa": (N, Reason.SECRET_NAME),
    "Keys/backup.kdbx": (N, Reason.SECRET_NAME),
    "~$Договор_ред3.docx": (N, Reason.OS_ARTIFACT),
    "Thumbs.db": (N, Reason.OS_ARTIFACT),
    # a nested archive and ordinary files: metadata only in Slice 1
    "Archive/site-backup.zip": (N, Reason.KIND_NOT_ADMITTED),
    "Contracts/Договор_ред3.docx": (N, Reason.KIND_NOT_ADMITTED),
    "Photos/WhatsApp/IMG-20251120-WA0001.jpg": (N, Reason.KIND_NOT_ADMITTED),
    "Media/voice memo.m4a": (N, Reason.KIND_NOT_ADMITTED),
}


@pytest.fixture(scope="module")
def indexed():
    import asyncio

    async def run():
        repo = InMemoryDriveIndexRepository()
        out = await index_fixture(FixtureDrive.from_file(_TREE), repo, FakeEmbeddings(), lambda lat, lon: None,
                                  "fake/8", EvalClock(), throttle_pause_s=0)
        return repo, out

    return asyncio.run(run())


@pytest.mark.parametrize("path", sorted(EXPECTED))
def test_scope_and_reason(indexed, path):
    repo, _ = indexed
    rec = repo.records.get((EVAL_USER, item_id_for(path)))
    if EXPECTED[path] is None:
        assert rec is None, f"{path} must have no record"
    else:
        assert rec is not None, f"{path} has no record"
        assert (rec.scope, rec.reason) == EXPECTED[path]
        assert rec.has_card is (rec.scope is not Scope.INSIDE_OPAQUE)


def test_nothing_inside_an_opaque_root_is_carded_and_nothing_failed(indexed):
    repo, out = indexed
    assert out.statuses.get("error", 0) == 0
    assert all(repo.records[key].scope is not Scope.INSIDE_OPAQUE for key in repo.vectors)
    # of the 3 169 generated files only the vault, the invoices and the camera roll keep records, every one carded
    bulk = {item_id_for(p) for p in ("Vault", "Archive/Invoices 2024", "Archive/Camera roll")}
    kept = [r for r in repo.records.values() if r.parent_id in bulk]
    assert len(kept) == 2400 and all(r.has_card for r in kept)
    for folder in ("Dumps/site-mirror", "Projects/alek-site/src", "Projects/alek-site/node_modules/lodash",
                   "Projects/venv-tools/lib/python3.13/site-packages/requests"):
        assert not [r for r in repo.records.values() if r.parent_id == item_id_for(folder)], folder


async def test_git_in_a_later_round_rescopes_and_removes_carded_files():
    """§16: files carded in earlier completed rounds are removed by the rescope cascade when `.git` arrives later,
    and no document loses its card."""
    from .test_drive_indexing_service import World

    w = World()
    site = w.drive.add("site", folder=True)
    files = [w.drive.add(f"m{i}.py", parent=site) for i in range(30)]
    doc = w.drive.add("Lease.pdf")
    await w.settle()
    assert set(files) | {site, doc} <= w.carded()
    w.drive.add(".git", parent=site, folder=True)
    await w.settle()
    assert w.rec(site).scope is Scope.OPAQUE_ROOT and w.rec(site).reason is Reason.OPAQUE_ROOT
    assert all(w.rec(f) is None for f in files)
    assert w.carded() == {site, doc}
