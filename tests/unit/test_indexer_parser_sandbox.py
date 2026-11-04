"""The image's unprivileged parser user (R3 F3-1, USER_DRIVE_INDEXING_RFC §9, §24.14): the Dockerfile creates the user
it names in DRIVE_PARSER_USER and refuses world-writable app or library files. The indexer itself runs no parser or
media child any more: documents and recordings go through the sandbox light service (UNTRUSTED_CONTENT_SANDBOX_RFC O14,
§7 S5), so it looks up no parser user; the image keeps the user until S6-a (one image serves every runtime, §12.6)."""
import re
from pathlib import Path

from src.adapters.subprocess_content_extractor import PARSER_USER_ENV

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def test_the_image_creates_the_user_it_names():
    text = DOCKERFILE.read_text()
    named = re.findall(rf"^ENV {PARSER_USER_ENV}=(\S+)\s*$", text, re.MULTILINE)
    assert len(named) == 1
    user = named[0]
    create = next(line for line in text.splitlines() if "useradd" in line)
    assert create.rstrip().endswith(f" {user}")
    assert "--system" in create and "--no-create-home" in create and "nologin" in create
    # the service itself stays root: switching the child to the parser user needs it
    assert not re.search(r"^USER\s", text, re.MULTILINE)


def test_the_image_build_refuses_world_writable_app_or_library_files():
    text = DOCKERFILE.read_text()
    copy_all = text.index("COPY . .")
    check = text.index("-perm -o+w")
    assert check > copy_all                                    # checked after the source is in place
    assert "/app" in text[check - 200:check + 200] and "site-packages" in text[check - 400:check + 400]
