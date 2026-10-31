"""Policy and scope over a hostile tree (USER_DRIVE_INDEXING_RFC §17.2, §5.2, §5.5; RFC Δ16).

The real tick runs over the eval fixture (tests/fixtures/drive_eval/tree.json) through the eval's FixtureDrive,
imported as a namespace package like tests/unit/validation/test_price_consensus.py. Every listed path has the
(scope, reason) the RFC prescribes; a path mapped to None must have no record at all. Content-stage classes
(machine text, bombs, encrypted files) are Slice 2 and are not in this table; since the flip (§24.1) the tick runs
with the fake content ports (tests/unit/services/drive_content_fakes.py), so an admitted file shows the outcome of
its content run.
"""
from pathlib import Path

import pytest

from scripts.validation.drive_eval.eval_connection import EVAL_CONNECTION_ID
from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from scripts.validation.drive_eval.runner import EvalClock, index_fixture
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_extraction import PARSER, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import Reason, RecordState, Scope

from ..scripts.drive_eval_samples import fake_content
from .drive_index_fakes import FakeEmbeddings

_TREE = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval" / "tree.json"
N, OR, IO = Scope.NORMAL, Scope.OPAQUE_ROOT, Scope.INSIDE_OPAQUE
_DOCX = "Договір_ред3.docx"
_DOCX_TEXT = ("Договір оренди нежитлового приміщення № 3. Орендодавець передає, а орендар приймає у тимчасове "
              "користування офісне приміщення загальною площею 48 квадратних метрів. Орендна плата становить "
              "1 250 євро на місяць і сплачується до п'ятого числа кожного місяця. Договір діє до 31 грудня 2026 "
              "року. Сторони можуть розірвати договір, письмово повідомивши іншу сторону за тридцять днів. ") * 3

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
    # an extracted repository without .git: only the tool-made folders collapse (RFC Δ16)
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
    ".DS_Store": (N, Reason.OS_ARTIFACT),          # an OS artefact before it is a dot-name
    # density over content class, decided from a probe (§5.5 class 1)
    "Dumps/site-mirror": (OR, Reason.DENSE_TREE),
    "Dumps/site-mirror/page-000.bin": None,
    "Vault": (N, Reason.FOLDER),                       # 800 small notes: documents, never dense
    "Vault/note-799.md": (N, None),                    # a note with text: indexed (§24.1)
    "Archive/Invoices 2024": (N, Reason.FOLDER),       # 600 PDFs
    "Archive/Camera roll": (N, Reason.FOLDER),         # 1 000 photos: media, never dense
    "Archive/Camera roll/IMG_0417.HEIC": (N, Reason.PHOTOGRAPH),   # no rendition: a photograph (§24.5)
    # never-download names (§5.1): they keep a card with their reason, except OS and lock artefacts (record only)
    "Keys/id_rsa": (N, Reason.SECRET_NAME),
    "Keys/backup.kdbx": (N, Reason.SECRET_NAME),
    "~$Договір_ред3.docx": (N, Reason.OS_ARTIFACT),
    "Thumbs.db": (N, Reason.OS_ARTIFACT),
    # a nested archive and ordinary files: an archive stays metadata only; documents and images are read; audio
    # waits, pending, for the transcript stage (policy 3, §27.3), which this tick does not run
    "Archive/site-backup.zip": (N, Reason.KIND_NOT_ADMITTED),
    "Contracts/Договір_ред3.docx": (N, None),          # a real DOCX: the parser reads its text, indexed (§24.1)
    "Photos/WhatsApp/IMG-20251120-WA0001.jpg": (N, Reason.PHOTOGRAPH),   # no rendition: a photograph (§24.5)
    "Media/voice memo.m4a": (N, None),
}


@pytest.fixture(scope="module")
def indexed():
    import asyncio

    async def run():
        repo = InMemoryDriveIndexRepository()
        content = fake_content()
        # the fake parser decodes raw bytes; a DOCX gets the text a real parser reads from it (§8.1)
        content["extractors"][PARSER].outcomes[_DOCX] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=_DOCX_TEXT)
        out = await index_fixture(FixtureDrive.from_file(_TREE), repo, FakeEmbeddings(), lambda lat, lon: None,
                                  "fake/8", EvalClock(), throttle_pause_s=0, **content)
        return repo, out

    return asyncio.run(run())


@pytest.mark.slow
@pytest.mark.parametrize("path", sorted(EXPECTED))
def test_scope_and_reason(indexed, path):
    repo, _ = indexed
    rec = repo.records.get((EVAL_CONNECTION_ID, item_id_for(path)))
    if EXPECTED[path] is None:
        assert rec is None, f"{path} must have no record"
    else:
        assert rec is not None, f"{path} has no record"
        assert (rec.scope, rec.reason) == EXPECTED[path]
        assert rec.has_card is (rec.scope is not Scope.INSIDE_OPAQUE and rec.reason is not Reason.OS_ARTIFACT)


@pytest.mark.slow
def test_a_voice_memo_waits_unread_for_the_transcript_stage(indexed):
    """Policy 3 (§27.3): the memo is pending with its card, and nothing was read from it — no readable generation,
    no chunks, no extractor link (the transcript stage, not wired here, is media's only reader)."""
    repo, _ = indexed
    rec = repo.records[(EVAL_CONNECTION_ID, item_id_for("Media/voice memo.m4a"))]
    assert rec.state is RecordState.PENDING and rec.has_card
    assert (rec.indexed_generation, rec.chunk_count, rec.extracted_by, rec.attempts) == (None, 0, None, 0)
    assert (rec.due_at, rec.priority) == (None, None)


@pytest.mark.slow
def test_a_docx_is_indexed(indexed):
    repo, _ = indexed
    rec = repo.records[(EVAL_CONNECTION_ID, item_id_for("Contracts/" + _DOCX))]
    assert rec.state is RecordState.INDEXED and rec.indexed_generation is not None and rec.chunk_count > 0


@pytest.mark.slow
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
