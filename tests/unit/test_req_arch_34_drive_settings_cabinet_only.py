"""REQ-ARCH-34 — drive settings are changed only in the Cabinet (MULTI_DRIVE_RFC §14 row 2b, T-12; slice 2b P-F).

1. A call of `rename`, `set_searched_by_default`, `set_scope`, `set_write_folder`, `set_primary`, `set_indexing`,
   `set_transcribe` or `disconnect` on a drive connection service, and `DriveDisconnectService.disconnect`, appears in
   `src/` only in `src/web/user_cabinet_app.py` and `src/services/drive_disconnect_service.py` (AST; the connection service's own file is skipped, it calls its repository; a receiver is
   a connection service when its dotted name mentions `connection`, or a disconnect service when it mentions
   `disconnect`). `scripts/` may call them: it is the owner's own tool, not a chat path.
2. The drive specialist's tools (`drive_tool_declarations`, both note tool sets) include no settings writer.
"""
import ast
from pathlib import Path
from typing import Iterator, List, Tuple

from src.domain.drive_note_tool_texts import NOTE_TOOLS, drive_note_tool_declarations
from src.domain.drive_tool_texts import drive_tool_declarations

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
ALLOWED = {"src/web/user_cabinet_app.py", "src/services/drive_disconnect_service.py"}
DEFINING = "src/services/drive_connection_service.py"   # its calls are on its own repository, not on the service
SETTINGS_WRITERS = frozenset({"rename", "set_searched_by_default", "set_scope", "set_write_folder", "set_primary",
                              "set_indexing", "set_transcribe", "disconnect"})
RECEIVER_WORDS = ("connection", "disconnect")


def _dotted(node: ast.AST) -> str:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _writer_calls(tree: ast.AST) -> Iterator[Tuple[int, str]]:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in SETTINGS_WRITERS):
            receiver = _dotted(node.func.value).lower()
            if any(word in receiver for word in RECEIVER_WORDS):
                yield node.lineno, f"{receiver}.{node.func.attr}"


def test_settings_writers_are_called_only_from_the_cabinet_and_the_disconnect_flow():
    found = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED or rel == DEFINING:
            continue
        found += [f"{rel}:{line} {call}" for line, call in _writer_calls(ast.parse(path.read_text(encoding="utf-8")))]
    assert found == []


def test_the_scan_sees_the_allowed_callers():
    """The guard would pass vacuously if the receiver rule matched nothing: both allowed files hold a writer call."""
    for rel in ALLOWED:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        assert list(_writer_calls(tree)), rel


def test_the_drive_specialist_has_no_settings_tool():
    names = {t["name"] for t in drive_tool_declarations()}
    names |= {t["name"] for t in drive_note_tool_declarations(writes=True)} | set(NOTE_TOOLS)
    assert not names & SETTINGS_WRITERS
