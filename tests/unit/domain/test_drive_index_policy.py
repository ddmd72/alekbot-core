import pytest

from src.domain.drive_index import ContentClass, DriveChange, Reason
from src.domain.drive_index_policy import (
    ADMITTED_KINDS, card_rank_of, content_class_of, density_still_valid, is_dense, is_dot_name,
    is_generated_dir, is_marker_child, item_rule, kind_of, probe_from_children,
)


@pytest.mark.parametrize("name,mime,kind", [
    ("a.pdf", "application/pdf", "pdf"), ("a.DOCX", "", "docx"), ("a.xlsx", "", "xlsx"), ("a.ods", "", "ods"),
    ("a.md", "text/markdown", "markdown"), ("a.txt", "text/plain", "text"), ("a.csv", "", "csv"),
    ("a.html", "text/html", "html"), ("a.eml", "", "eml"), ("a.vtt", "", "subtitles"), ("a.rtf", "", "rtf"),
    ("a.epub", "", "epub"), ("a.heic", "image/heic", "image"), ("a.jpg", "", "image"), ("a.svg", "", "image"),
    ("a.m4a", "audio/mp4", "audio"), ("a.webm", "video/webm", "video"), ("a.zip", "", "archive"),
    ("a.py", "", "code"), ("a.log", "", "log"), ("a.sql", "", "data_dump"), ("a.jsonl", "", "data_dump"),
    ("a.sqlite", "", "database"), ("a.exe", "", "executable"), ("a.iso", "", "disk_image"),
    ("a.pages", "", "iwork"), ("a.bin", "application/octet-stream", "other"), ("noext", "", "other"),
])
def test_kind_of(name, mime, kind):
    assert kind_of(name, mime, is_folder=False) == kind


def test_folder_kind_and_classes():
    assert kind_of("x", "", is_folder=True) == "folder"
    assert content_class_of("pdf") is ContentClass.DOCUMENT
    assert content_class_of("markdown") is ContentClass.DOCUMENT
    assert content_class_of("image") is ContentClass.MEDIA
    assert content_class_of("video") is ContentClass.MEDIA
    assert content_class_of("code") is ContentClass.OTHER
    assert content_class_of("folder") is ContentClass.FOLDER
    assert [card_rank_of(c) for c in (ContentClass.DOCUMENT, ContentClass.MEDIA, ContentClass.OTHER,
                                      ContentClass.FOLDER)] == [0, 1, 2, 3]


def test_slice1_admits_nothing():
    assert ADMITTED_KINDS == frozenset()


@pytest.mark.parametrize("name,facets,reason", [
    (".env", {}, Reason.DOT_NAME), ("id_rsa", {}, Reason.SECRET_NAME), ("id_ed25519.pub", {}, Reason.SECRET_NAME),
    ("server.pem", {}, Reason.SECRET_NAME), ("tls.key", {}, Reason.SECRET_NAME), ("cert.p12", {}, Reason.SECRET_NAME),
    ("a.pfx", {}, Reason.SECRET_NAME), ("vault.kdbx", {}, Reason.SECRET_NAME), ("login.keychain-db", {}, Reason.SECRET_NAME),
    ("~$Договор.docx", {}, Reason.OS_ARTIFACT), ("Thumbs.db", {}, Reason.OS_ARTIFACT), ("desktop.ini", {}, Reason.OS_ARTIFACT),
    ("x.tmp", {}, Reason.OS_ARTIFACT), ("a.pdf", {"remoteItem": {}}, Reason.REMOTE_ITEM),
    ("a.pdf", {"package": {"type": "oneNote"}}, Reason.PACKAGE), ("a.pdf", {"malware": {}}, Reason.MALWARE),
    ("a.pdf", {}, None),
])
def test_item_rule(name, facets, reason):
    assert item_rule(DriveChange(item_id="i", name=name, facets=facets)) is reason


def test_markers_and_dot_names():
    assert is_marker_child(".git") and is_marker_child("pyvenv.cfg") and not is_marker_child("git")
    assert is_dot_name(".obsidian") and not is_dot_name("a.b")


def _kids(names):
    return [DriveChange(item_id=str(i), name=n) for i, n in enumerate(names)]


def test_probe_counts_classes_and_markers():
    probe = probe_from_children(_kids(["a.pdf", "b.jpg", "c.bin", ".git"]))
    assert probe.marker_found is True and probe.sampled == 4
    assert probe.class_counts[ContentClass.DOCUMENT] == 1 and probe.class_counts[ContentClass.MEDIA] == 1


def test_density_needs_count_and_mostly_other():
    others = probe_from_children(_kids([f"p{i}.bin" for i in range(200)]))
    pdfs = probe_from_children(_kids([f"p{i}.pdf" for i in range(200)]))
    photos = probe_from_children(_kids([f"p{i}.HEIC" for i in range(200)]))
    assert is_dense(700, others) is True
    assert is_dense(500, others) is False            # threshold is "exceeds"
    assert is_dense(600, pdfs) is False and is_dense(1000, photos) is False


def test_density_hysteresis_doubles_or_halves():
    assert density_still_valid(600, 1100) is True
    assert density_still_valid(600, 1200) is False
    assert density_still_valid(600, 301) is True
    assert density_still_valid(600, 300) is False
    assert density_still_valid(0, 600) is False


def test_generated_dirs_by_name():
    assert is_generated_dir("node_modules") and is_generated_dir("__pycache__") and is_generated_dir("site-packages")
    assert not is_generated_dir("modules") and not is_generated_dir("build")
