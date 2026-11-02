"""The drive eval scorer compares on the drive-relative path: cards print `<mount>/<path>` (multi-drive 2a), gold files
stay relative. A leading segment naming the eval connection's mount is dropped; a real tree path is left alone."""
from types import SimpleNamespace

from src.domain.drive_connection import DEFAULT_MOUNT_NAME
from scripts.validation.drive_eval.eval_connection import drive_relative
from scripts.validation.drive_eval.fixture_drive import FixtureDrive
from scripts.validation.drive_eval.runner import _card_paths

from .drive_eval_samples import MANIFEST

MOUNT = DEFAULT_MOUNT_NAME


def test_a_mount_prefix_is_stripped_to_the_gold_path():
    drive = FixtureDrive(MANIFEST)
    assert drive_relative(f"{MOUNT}/Contracts/Lease agreement — Porto.pdf", drive) == "Contracts/Lease agreement — Porto.pdf"
    assert drive_relative(f"{MOUNT.lower()}/Scans/Scan_0012.pdf", drive) == "Scans/Scan_0012.pdf"


def test_a_relative_path_and_the_bare_mount_stay_as_they_are():
    drive = FixtureDrive(MANIFEST)
    assert drive_relative("Contracts/Договор_ред1.docx", drive) == "Contracts/Договор_ред1.docx"
    assert drive_relative(f"{MOUNT}/", drive) == ""


def test_card_paths_are_drive_relative_including_copies():
    drive = FixtureDrive(MANIFEST)
    card = SimpleNamespace(ref="", path=f"{MOUNT}/Contracts/Lease agreement — Porto.pdf",
                           also_at=(f"{MOUNT}/Inbox/Lease agreement — Porto.pdf",))
    assert _card_paths(card, drive) == ["Contracts/Lease agreement — Porto.pdf", "Inbox/Lease agreement — Porto.pdf"]
