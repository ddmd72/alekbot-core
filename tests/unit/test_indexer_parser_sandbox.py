"""The indexer runs the parser child as the unprivileged user its image names (R3 F3-1, USER_DRIVE_INDEXING_RFC §9,
§24.14): indexer_main passes DRIVE_PARSER_USER to the parser; the Dockerfile creates that user and sets the variable,
so the deployed indexer always sandboxes, and a user missing from the container fails the start. Without the variable
(local development) the parser runs unsandboxed, with a WARNING. The main service never builds the parser."""
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters import subprocess_content_extractor as extractor_module
from src.adapters.subprocess_content_extractor import PARSER_USER_ENV
from src.composition import drive_indexing_setup
from src.domain.drive_extraction import PARSER

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def wired(monkeypatch):
    captured = {}
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv(PARSER_USER_ENV, raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       # the cipher's key name (SECRETS_AT_REST_RFC §2.6, I6); build_cipher calls no KMS API
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid", "ANTHROPIC_API_KEY": "ak"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", MagicMock())
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    monkeypatch.setattr(indexer_main, "DriveIndexingService", lambda *a, **kw: captured.update(kw) or MagicMock())
    return captured


def test_without_the_variable_the_parser_is_unsandboxed(wired):
    indexer_main.build_app()
    assert wired["extractors"][PARSER]._sandbox is None


def test_the_named_user_reaches_the_parser(wired, monkeypatch):
    monkeypatch.setenv(PARSER_USER_ENV, "driveparser")
    monkeypatch.setattr(extractor_module.pwd, "getpwnam",
                        lambda name: MagicMock(pw_name=name, pw_uid=999, pw_gid=998))
    monkeypatch.setattr(extractor_module, "_effective_uid", lambda: 0)
    indexer_main.build_app()
    assert wired["extractors"][PARSER]._sandbox == (999, 998)


def test_a_named_user_missing_from_the_container_fails_the_start(wired, monkeypatch):
    monkeypatch.setenv(PARSER_USER_ENV, "no-such-user-r3-f3-1")
    with pytest.raises(RuntimeError):
        indexer_main.build_app()


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
