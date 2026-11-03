"""Guard — nothing imports a moved file piece through its old module (EXTERNAL_FILES_RFC slice U1).

U1 moved the shared file pieces to source-neutral modules. An old module may still import a moved name for its own use,
so `from <old module> import <moved name>` keeps resolving; this scan (AST over `src/`, `tests/`, `scripts/`) fails on
each such import, or on an `<old module>.<moved name>` attribute read, and on a surviving `drive_sniff.py`.
Files that do not parse are skipped. `scripts/memory/` is skipped (gitignored data).
"""
import ast
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "tests", "scripts")
SKIP_PREFIXES = ("scripts/memory/",)
THIS_FILE = "tests/unit/test_moved_file_pieces_imports.py"

# old module (dotted) -> the names that moved out of it (the new home is in the comment)
MOVED: Dict[str, FrozenSet[str]] = {
    # -> src.domain.document_extraction
    "src.domain.drive_extraction": frozenset({
        "PARSER", "READER", "EXTRACTOR_CHAINS", "chain_for", "ExtractionStatus", "text_digest", "failure_label",
        "garbage_share", "model_route", "_GARBAGE"}),
    # -> src.domain.document_kind
    "src.domain.drive_index_policy": frozenset({
        "_EXT_KIND", "_PACKAGE_EXT", "_PACKAGED_EXTS", "_MIME_KIND", "_MIME_TEXT_KINDS", "_MIME_TABLE_KINDS",
        "mime_kind", "_effective_ext", "_kind_from_ext", "mime_decides_kind", "kind_of"}),
    "src.domain.drive_image_mime": frozenset({"image_mime"}),
    # -> src.domain.document_limits
    "src.domain.drive_index_params": frozenset({"OPEN_MAX_CHARS"}),
    "src.domain.user_drive": frozenset({
        "MAX_DRIVE_DOWNLOAD_BYTES", "MAX_DRIVE_VISION_IMAGE_BYTES", "MAX_DRIVE_VISION_PDF_BYTES"}),
    "src.domain.text_data_uris": frozenset({"MAX_TEXT_EXPORT_BYTES"}),
    # -> src.domain.untrusted_text
    "src.domain.drive_search": frozenset({
        "_NONCE", "_char_class", "_INVISIBLE", "_OPENER", "_SLASH", "_UNDERSCORE", "_tag_letter", "_GAP",
        "_WRAPPER_TAG", "REFERENCE_REMINDER", "REFERENCE_REMINDER_ATTACHED", "_escape", "escape_text",
        "wrap_untrusted"}),
}
DELETED_MODULE = "src.domain.drive_sniff"   # -> src.domain.document_kind


def _files() -> Iterator[Path]:
    for top in SCAN_DIRS:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(SKIP_PREFIXES) or rel == THIS_FILE:
                continue
            yield path


def _module_of(rel: str) -> str:
    return rel[:-3].replace("/", ".")


def _resolve(node: ast.ImportFrom, own_module: str, is_package: bool) -> Optional[str]:
    if not node.level:
        return node.module
    base = own_module.split(".")
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _offenders(path: Path) -> List[str]:
    rel = path.relative_to(ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return []
    own = _module_of(rel)
    found: List[str] = []
    aliases: Dict[str, str] = {}   # local name -> old module it stands for
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = _resolve(node, own, path.name == "__init__.py")
            if source is None:
                continue
            for alias in node.names:
                if source == DELETED_MODULE:
                    found.append(f"{rel}:{node.lineno}  imports from deleted {DELETED_MODULE}")
                elif source in MOVED and alias.name in MOVED[source] and own != source:
                    found.append(f"{rel}:{node.lineno}  from {source} import {alias.name}")
                elif f"{source}.{alias.name}" in MOVED or f"{source}.{alias.name}" == DELETED_MODULE:
                    aliases[alias.asname or alias.name] = f"{source}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == DELETED_MODULE:
                    found.append(f"{rel}:{node.lineno}  imports deleted {DELETED_MODULE}")
                elif alias.name in MOVED and alias.asname:
                    aliases[alias.asname] = alias.name
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and aliases.get(node.value.id) in MOVED and node.attr in MOVED[aliases[node.value.id]]):
            found.append(f"{rel}:{node.lineno}  {node.value.id}.{node.attr} (read on {aliases[node.value.id]})")
    return found


def test_no_import_reaches_a_moved_name_through_its_old_module():
    offenders = [line for path in _files() for line in _offenders(path)]
    assert not offenders, "moved names imported through their old module:\n" + "\n".join(offenders)


def test_drive_sniff_module_is_gone():
    assert not (ROOT / "src" / "domain" / "drive_sniff.py").exists()
