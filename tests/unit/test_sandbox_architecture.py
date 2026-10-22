"""The sandbox server and its clients import no document, image or archive library, open no outbound connection and never
decode a reply body (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6, §4.10; plan G21)."""
import pytest
import ast
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

pytestmark = pytest.mark.datafile

_ROOT = Path(__file__).resolve().parents[2]

_SERVER_FILES = (
    [_ROOT / "sandbox_main.py"]
    + sorted((_ROOT / "src" / "web").glob("sandbox_*.py"))
    + sorted((_ROOT / "src" / "composition").glob("sandbox_*.py"))
)
_CLIENT_FILES = sorted((_ROOT / "src" / "adapters" / "sandbox_client").glob("*.py"))
_ENGINE_FILES = sorted((_ROOT / "src" / "adapters" / "sandbox_engine").glob("*.py"))

_PARSERS = {"PIL", "fitz", "pypdf", "docx", "openpyxl", "markitdown", "bs4", "lxml", "zipfile", "tarfile", "gzip", "bz2",
            "lzma", "xml.etree", "defusedxml"}
_OUTBOUND = {"socket", "httpx", "aiohttp", "requests", "urllib", "http.client"}
# The one documented exception (G21): the in-app OIDC verification fetches Google's public signing keys on every
# verification, from the server process, never from an engine. It carries no payload.
_VERIFIER_EXCEPTION = "google.auth.transport.requests"


def _imports(path: Path) -> Iterator[Tuple[str, int]]:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield node.module or "", node.lineno
            for a in node.names:
                yield f"{node.module}.{a.name}", node.lineno


def _hits(files, banned: Set[str]):
    found = []
    for f in files:
        for name, line in _imports(f):
            if any(name == b or name.startswith(b + ".") for b in banned):
                found.append(f"{f.relative_to(_ROOT)}:{line} imports {name}")
    return found


def test_the_file_sets_are_not_empty():
    assert len(_SERVER_FILES) >= 8 and len(_CLIENT_FILES) >= 3 and len(_ENGINE_FILES) >= 4


def test_server_and_client_import_no_document_image_or_archive_library():
    assert _hits(_SERVER_FILES + _CLIENT_FILES, _PARSERS) == []


def test_engine_modules_import_no_parser_library_either():
    """The render child shells out to node; it parses nothing itself."""
    assert _hits(_ENGINE_FILES, _PARSERS) == []


# Documented exceptions (G2): the failure text of a script is a capped text/plain body, decoded to a string and never
# parsed; the token provider reads the `exp` claim of the client's OWN identity token, not a reply.
_TEXT_FAILURE_DECODE = ("sandbox_docx_runner.py", "reply.body.decode('utf-8', errors='replace')")
_OWN_TOKEN_FILE = "metadata_id_token_provider.py"


def test_clients_do_not_decode_response_bodies():
    """The outcome header decoder lives in domain; a client moves bytes and never parses a reply body."""
    bad = []
    for f in _CLIENT_FILES:
        for node in ast.walk(ast.parse(f.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner, attr = node.func.value, node.func.attr
            if attr == "loads" and isinstance(owner, ast.Name) and owner.id == "json" and f.name != _OWN_TOKEN_FILE:
                bad.append(f"{f.name}:{node.lineno} json.loads")
            if attr == "json":
                bad.append(f"{f.name}:{node.lineno} .json()")
            if attr == "decode" and (f.name, ast.unparse(node)) != _TEXT_FAILURE_DECODE:
                bad.append(f"{f.name}:{node.lineno} .decode()")
    assert bad == []


def test_render_service_code_opens_no_outbound_connection():
    server_and_engine = [f for f in _SERVER_FILES + _ENGINE_FILES]
    found = _hits(server_and_engine, _OUTBOUND)
    assert found == [], found


def test_the_verifiers_key_fetch_is_the_one_documented_outbound_call():
    """`google.auth.transport.requests` appears only in the OIDC verifier module, outside every engine (G21)."""
    users: Dict[str, int] = {}
    for f in _SERVER_FILES + _ENGINE_FILES + _CLIENT_FILES + [_ROOT / "src" / "web" / "worker_oidc_verifier.py"]:
        for name, _ in _imports(f):
            if name == _VERIFIER_EXCEPTION or name.startswith(_VERIFIER_EXCEPTION):
                users[f.name] = users.get(f.name, 0) + 1
    assert set(users) <= {"worker_oidc_verifier.py", "metadata_id_token_provider.py", "sandbox_http_client.py"}, users
    assert not any(f.parent.name == "sandbox_engine" for f in _ENGINE_FILES if f.name in users)
