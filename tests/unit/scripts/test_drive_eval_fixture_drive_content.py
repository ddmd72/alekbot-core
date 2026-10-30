"""FixtureDrive serves file bytes, renditions and the Step 1 reads (USER_DRIVE_INDEXING_RFC §8.1, §24.5, §17.8)."""
import io

import pytest

from scripts.validation.drive_eval.fixture_drive import ROOT_ID, FixtureDrive, item_id_for, render_template
from src.domain.user_drive import DriveFileTooLarge, DriveItemNotFoundError

USER = "u"
PDF = "application/pdf"


def _manifest():
    return {
        "items": [
            {"path": "Docs", "folder": True},
            {"path": "Docs/lease.pdf", "folder": False, "mime": PDF, "size": 999999, "content": "files/lease.pdf"},
            {"path": "Docs/empty.md", "folder": False, "mime": "text/markdown", "size": 10},
            {"path": "Pics/receipt.jpg", "folder": False, "mime": "image/jpeg", "size": 5, "content": "files/r.jpg"},
            {"path": "Pics/IMG_1.HEIC", "folder": False, "mime": "image/heic", "size": 3000,
             "thumbnail": "files/thumb.jpg"},
        ],
        "generate": [
            {"folder": "Vault", "pattern": "note-{n:03d}.md", "count": 3, "mime": "text/markdown", "size": 1,
             "content_template": "vault_note"},
            {"folder": "Bills", "pattern": "invoice-{n:03d}.pdf", "count": 2, "mime": PDF, "size": 1,
             "content_template": "invoice_2024"},
        ],
    }


@pytest.fixture
def drive(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "lease.pdf").write_bytes(b"%PDF-1.4 lease bytes")
    (files / "r.jpg").write_bytes(b"\xff\xd8\xffreceipt")
    (files / "thumb.jpg").write_bytes(b"\xff\xd8\xffthumb")
    return FixtureDrive(_manifest(), base_dir=tmp_path)


async def test_an_items_size_is_its_files_length(drive):
    lease = await drive.get_change(USER, item_id_for("Docs/lease.pdf"))
    assert lease.size_bytes == len(b"%PDF-1.4 lease bytes")
    empty = await drive.get_change(USER, item_id_for("Docs/empty.md"))
    assert empty.size_bytes == 10          # no bytes: the declared size stays


async def test_download_to_file_writes_the_bytes(drive, tmp_path):
    dest = tmp_path / "out.pdf"
    n = await drive.download_to_file(USER, item_id_for("Docs/lease.pdf"), str(dest), max_bytes=1000)
    assert n == dest.stat().st_size and dest.read_bytes() == b"%PDF-1.4 lease bytes"
    assert drive.downloads == [item_id_for("Docs/lease.pdf")]


async def test_a_cut_stream_raises_after_writing_what_fits(drive, tmp_path):
    dest = tmp_path / "out.pdf"
    with pytest.raises(DriveFileTooLarge) as exc:
        await drive.download_to_file(USER, item_id_for("Docs/lease.pdf"), str(dest), max_bytes=4)
    assert exc.value.limit_bytes == 4 and dest.read_bytes() == b"%PDF"


async def test_an_item_without_bytes_downloads_empty(drive, tmp_path):
    dest = tmp_path / "out.md"
    assert await drive.download_to_file(USER, item_id_for("Docs/empty.md"), str(dest), max_bytes=100) == 0
    assert dest.read_bytes() == b""


@pytest.mark.parametrize("path", ["Docs", "Nowhere/x.pdf"])
async def test_a_folder_or_a_missing_item_is_not_found(drive, tmp_path, path):
    with pytest.raises(DriveItemNotFoundError):
        await drive.download_to_file(USER, item_id_for(path), str(tmp_path / "x"), max_bytes=100)


async def test_renditions(drive):
    """`max_edge_px` is the rendition's longer edge in pixels (MULTI_DRIVE_RFC §6): 176 is the old "medium"."""
    assert await drive.get_thumbnail(USER, item_id_for("Pics/IMG_1.HEIC"), 176) == b"\xff\xd8\xffthumb"
    assert await drive.get_thumbnail(USER, item_id_for("Pics/IMG_1.HEIC"), 2048) == b"\xff\xd8\xffthumb"
    assert await drive.get_thumbnail(USER, item_id_for("Pics/receipt.jpg"), 176) == b"\xff\xd8\xffreceipt"
    assert await drive.get_thumbnail(USER, item_id_for("Docs/lease.pdf"), 176) is None
    with pytest.raises(DriveItemNotFoundError):
        await drive.get_thumbnail(USER, "fx-unknown", 176)
    assert drive.thumbnail_calls[0] == (item_id_for("Pics/IMG_1.HEIC"), 176)


async def test_templates_render_per_item(drive, tmp_path):
    from pdfminer.high_level import extract_text

    note = tmp_path / "n.md"
    await drive.download_to_file(USER, item_id_for("Vault/note-002.md"), str(note), max_bytes=10_000)
    assert note.read_text(encoding="utf-8").startswith("# Note 002")
    bill = tmp_path / "b.pdf"
    size = await drive.download_to_file(USER, item_id_for("Bills/invoice-001.pdf"), str(bill), max_bytes=100_000)
    assert bill.read_bytes().startswith(b"%PDF") and size == bill.stat().st_size
    assert "INV-2024-001" in extract_text(io.BytesIO(bill.read_bytes()))
    assert render_template("invoice_2024", 1, "invoice-001.pdf") == bill.read_bytes()   # deterministic


def test_file_bytes_need_a_base_dir():
    with pytest.raises(ValueError, match="base_dir"):
        FixtureDrive(_manifest())


def test_the_content_hash_moves_with_any_served_byte(tmp_path, drive):
    same = FixtureDrive(_manifest(), base_dir=tmp_path)
    assert same.content_sha256() == drive.content_sha256()
    (tmp_path / "files" / "thumb.jpg").write_bytes(b"\xff\xd8\xffother")
    assert FixtureDrive(_manifest(), base_dir=tmp_path).content_sha256() != drive.content_sha256()


async def test_step1_reads_for_the_dry_run(drive):
    root = await drive.get_root(USER)
    assert root.item_id == ROOT_ID and root.is_folder and root.path == ""
    top = await drive.list_children(USER, ROOT_ID)
    assert [i.name for i in top] == ["Bills", "Docs", "Pics", "Vault"] and all(i.is_folder for i in top)
    docs = await drive.list_children(USER, item_id_for("Docs"))
    assert [(i.name, i.path) for i in docs] == [("empty.md", "Docs/empty.md"), ("lease.pdf", "Docs/lease.pdf")]
    lease = await drive.get_item(USER, item_id_for("Docs/lease.pdf"))
    assert lease.mime_type == PDF and lease.size_bytes == len(b"%PDF-1.4 lease bytes")
    assert await drive.download(USER, lease.item_id) == b"%PDF-1.4 lease bytes"
    with pytest.raises(DriveItemNotFoundError):
        await drive.get_item(USER, "fx-unknown")
    with pytest.raises(DriveItemNotFoundError):
        await drive.list_children(USER, "fx-unknown")
    with pytest.raises(NotImplementedError):
        await drive.delete(USER, lease.item_id)
