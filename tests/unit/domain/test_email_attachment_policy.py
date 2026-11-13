"""Email attachment policy: the noise filter, the readable kinds, the sniffed kind and the input cut."""
import io
import zipfile

import pytest

from src.domain.document_limits import parse_input_cap
from src.domain.document_read import PARSE_KINDS
from src.domain.email import EmailPart
from src.domain.email_attachment_policy import (
    ATTACHMENT_HEAD_CHARS, ATTACHMENT_TAIL_CHARS, MAIL_READ_KINDS, MIN_ATTACHMENT_BYTES, NOISE_EXTS, NOISE_MIMES,
    declared_kind, declared_mime, input_slices, is_noise, noise_reason, skip_before_download, sniffed_kind,
)


def part(name, mime="application/pdf", size=20_000, inline=False):
    return EmailPart(part_id="1", filename=name, mime=mime, size=size, handle="h", displayed_inline=inline)


@pytest.mark.parametrize("ext", sorted(NOISE_EXTS))
def test_every_noise_extension_is_noise(ext):
    assert is_noise(part(f"contact{ext}", mime="application/octet-stream"))


@pytest.mark.parametrize("mime", sorted(NOISE_MIMES))
def test_every_noise_mime_is_noise_without_an_extension(mime):
    assert is_noise(part("attachment", mime=mime))


@pytest.mark.parametrize("name,mime", [("photos.zip", "application/zip"), ("voice_note.m4a", "audio/mp4"),
                                       ("clip.mp3", "audio/mpeg"), ("clip.mp4", "video/mp4"),
                                       ("page.html", "text/html"), ("page.htm", "")])
def test_archives_media_and_html_are_noise(name, mime):
    assert is_noise(part(name, mime=mime))


def test_the_size_floor_is_exact():
    assert is_noise(part("LAB-2025-03-0417.pdf", size=MIN_ATTACHMENT_BYTES - 1))
    assert not is_noise(part("LAB-2025-03-0417.pdf", size=MIN_ATTACHMENT_BYTES))
    assert MIN_ATTACHMENT_BYTES == 2_048


def test_an_inline_image_is_noise_but_an_inline_document_is_not():
    assert noise_reason(part("logo.png", mime="image/png", inline=True)) == "inline_image"
    assert not is_noise(part("RM-55120.pdf", inline=True))


def test_a_displayed_image_attachment_is_not_noise():
    assert not is_noise(part("photo.jpg", mime="image/jpeg"))


def test_a_document_is_not_noise():
    assert noise_reason(part("budget_2026.xlsx", mime="")) == ""


def test_skip_before_download_decides_image_and_size_only():
    cap = parse_input_cap("email")
    assert skip_before_download(part("photo.jpg", mime="image/jpeg")) == "image"
    assert skip_before_download(part("LAB-2025-03-0417.pdf", size=cap)) == ""
    assert skip_before_download(part("LAB-2025-03-0417.pdf", size=cap + 1)) == "too_large"


def test_declared_mime_drops_parameters_and_falls_back_to_the_name():
    assert declared_mime(part("a.pdf", mime="Application/PDF; name=a.pdf")) == "application/pdf"
    assert declared_mime(part("expenses_2025.csv", mime="")) == "text/csv"
    assert declared_mime(part("blob", mime="")) == "application/octet-stream"
    assert declared_kind(part("expenses_2025.csv", mime="text/csv")) == "csv"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _xlsx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")
    return buf.getvalue()


def test_the_bytes_decide_the_kind():
    assert sniffed_kind(PNG, part("LAB-2025-03-0417.pdf")) == "image"
    assert sniffed_kind(b"%PDF-1.7\n" + b"x" * 40, part("notes.txt", mime="text/plain")) == "pdf"
    assert sniffed_kind(_xlsx(), part("budget_2026.xlsx", mime="")) == "xlsx"


def test_the_read_kinds_are_parseable_and_exclude_legacy_word():
    assert MAIL_READ_KINDS <= PARSE_KINDS
    assert "doc" not in MAIL_READ_KINDS


def test_a_long_text_is_cut_into_head_and_tail():
    text = "\n\n".join(f"Paragraph {i} of the lab report. " + "word " * 40 for i in range(120))
    assert len(text) > 20_000 - 5_000
    head, tail = input_slices("pdf", text)
    assert 0 < len(head) <= ATTACHMENT_HEAD_CHARS and 0 < len(tail) <= ATTACHMENT_TAIL_CHARS
    assert head.startswith("Paragraph 0") and tail.endswith(text.strip()[-20:])


def test_a_short_text_is_its_own_head():
    text = "Lab report. " * 250
    assert len(text) == 3_000
    assert input_slices("pdf", text) == (text.strip(), "")


def test_a_sheet_keeps_its_header_and_first_twenty_rows_and_no_tail():
    rows = "\n".join(f"| 2026-01-{i:02d} | item {i} | {i} |" for i in range(1, 31))
    text = f"## Budget\n| date | item | amount |\n| --- | --- | --- |\n{rows}"
    head, tail = input_slices("xlsx", text)
    assert tail == ""
    assert "| date | item | amount |" in head and "item 20 |" in head and "item 21 |" not in head
