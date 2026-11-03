"""The one-time link's pure rules (DRIVE_MCP_CONNECTOR_RFC §8): the read limit, liveness, headers, the base URL."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_link
from src.domain.drive_link import DriveLink, link_base_url, link_headers

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
SAFETY = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}


def link(reads: int = 0) -> DriveLink:
    return DriveLink(link_key="k", user_id="u", account_id="a", connection_id="c", ref_tag="gd", item_id="i",
                     grant_id="g", created_at=T0, expires_at=T0 + timedelta(seconds=300),
                     purge_after=T0 + timedelta(days=1), reads=reads)


def test_the_max_reads_is_one_and_never_above_three():
    # owner 2026-11-03: N = 1 until a client's pre-fetch is measured; never above 3
    assert drive_link.LINK_MAX_READS == 1
    assert 1 <= drive_link.LINK_MAX_READS <= 3
    assert drive_link.LINK_TTL_S == 300


def test_live_until_expiry_exclusive_and_until_the_reads_run_out():
    assert link().live(T0 + timedelta(seconds=299), 1)
    assert not link().live(T0 + timedelta(seconds=300), 1)
    assert not link(reads=1).live(T0, 1)
    assert link(reads=2).live(T0, 3)
    assert not link(reads=3).live(T0, 3)


@pytest.mark.parametrize("name,mime", [
    ("12 Oak Street.png", "image/png"),
    ("photo.jpg", "image/jpeg"),
    ("anim.gif", "image/gif"),
    ("pic.webp", "image/webp"),
    ("FA-0137.pdf", "application/pdf"),
])
def test_png_jpeg_gif_webp_and_pdf_open_inline(name, mime):
    headers = link_headers(name, mime)
    assert headers["Content-Type"] == mime
    assert headers["Content-Disposition"].startswith("inline; ")
    assert f'filename="{name}"' in headers["Content-Disposition"]


@pytest.mark.parametrize("mime", [
    "image/svg+xml", "text/html", "text/html; charset=utf-8", "application/xhtml+xml", "text/xml", "application/xml",
    "text/javascript", "application/javascript", "application/rss+xml", "",
])
def test_svg_html_xml_and_js_are_an_octet_stream_attachment(mime):
    headers = link_headers("plan.svg", mime)
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Content-Disposition"].startswith("attachment; ")


def test_a_docx_is_an_attachment_with_its_own_type():
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    headers = link_headers("Tom's plan.docx", docx)
    assert headers["Content-Type"] == docx
    assert headers["Content-Disposition"] == (
        "attachment; filename=\"Tom's plan.docx\"; filename*=UTF-8''Tom%27s%20plan.docx")


@pytest.mark.parametrize("name,mime", [
    ("FA-0137.pdf", "application/pdf"), ("Tom's plan.docx", "application/msword"), ("x.html", "text/html"),
    ("standup.m4a", "audio/mp4"),
])
def test_every_header_set_has_no_store_no_referrer_nosniff_and_the_sandbox_csp(name, mime):
    headers = link_headers(name, mime)
    assert {k: headers[k] for k in SAFETY} == SAFETY


def test_a_non_ascii_name_gets_filename_star():
    headers = link_headers('Ferretería El Tornillo Feliz "FA-0137".pdf', "application/pdf")
    disposition = headers["Content-Disposition"]
    assert "filename*=UTF-8''Ferreter%C3%ADa%20El%20Tornillo%20Feliz%20%22FA-0137%22.pdf" in disposition
    fallback = disposition.split('filename="', 1)[1].split('"', 1)[0]
    assert fallback.isascii() and fallback == "Ferreter_a El Tornillo Feliz _FA-0137_.pdf"
    assert all(ord(c) < 128 for c in disposition)


def test_the_base_url_is_the_resource_host():
    assert link_base_url("https://mcp.example.test/mcp") == "https://mcp.example.test"
    assert link_base_url("http://localhost:8080/mcp/") == "http://localhost:8080"
    with pytest.raises(ValueError):
        link_base_url("/mcp")


@pytest.mark.parametrize("mime", ["text/plain,text/html", "text/plain\nX-Injected: 1", "image/png text/html",
                                  "image/png\r", "application", "/pdf", "image/p ng", "IMAGE/PNG!"])
def test_a_type_that_is_not_one_plain_type_subtype_is_an_octet_stream_attachment(mime):
    headers = link_headers("FA-0137.pdf", mime)
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Content-Disposition"].startswith("attachment; ")


def test_a_plain_type_with_parameters_keeps_its_type():
    assert link_headers("FA-0137.pdf", "Application/PDF; charset=binary")["Content-Type"] == "application/pdf"
