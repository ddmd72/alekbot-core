"""Building-block READMEs must not reference code that no longer exists.

Every `docs/05_building_blocks/*/*.md` that starts with the `<!-- block` header is checked: each `covers:` line
matches a tracked file, each backticked repository path exists, each `path::Symbol` / `path::Class.member` names
a real definition (Python files are parsed with `ast`), and relative `.md` links resolve. A stale reference
misleads the reader the README exists to orient (docs/04_solution_strategy/decisions/building_block_docs_contract.md).
READMEs without the header are in the old format and are skipped on purpose.
"""
import ast
import fnmatch
import glob
import re
import subprocess
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BLOCKS_DIR = ROOT / "docs" / "05_building_blocks"
HEADER = "<!-- block"

# Real repository trees; a word in backticks starting with one of these is a path.
PATH_PREFIXES = (
    "src/", "tests/", "docs/", "scripts/", "config/", "contracts/", "prompts_snapshot/", "firestore_utils/",
    "docx_generator/", "pdf_generator/", ".github/",
)
# Gitignored trees: they exist on the owner's machine only and are never checked.
UNTRACKED_PREFIXES = ("scripts/memory/", "docs/superpowers/", "firestore_utils/uploads/")

_SYMBOL = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$")


def tracked_files(root: Path) -> list[str]:
    """Repository files as posix paths: `git ls-files`, or a filesystem walk when git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True, timeout=30
        ).stdout
        files = [line for line in out.splitlines() if line]
        if files:
            return files
    except (OSError, subprocess.SubprocessError):
        pass
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".codegraph"}
    return [
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not skip.intersection(p.relative_to(root).parts)
    ]


def parse_header(text: str) -> tuple[list[str], str | None, str] | None:
    """(covers lines, audited value or None, body without the header) for a README with the block header; else None.

    The optional `audited: YYYY-MM-DD` line ends the covers list wherever it sits and is never a covers path.
    """
    if not text.startswith(HEADER):
        return None
    end = text.find("-->")
    if end == -1:
        return [], None, text
    header, body = text[:end], text[end + 3:]
    covers: list[str] = []
    audited: str | None = None
    in_covers = False
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("audited:"):
            audited = stripped[len("audited:"):].strip()
            in_covers = False
            continue
        if stripped.startswith("covers:"):
            in_covers = True
            stripped = stripped[len("covers:"):].strip()
        if in_covers and stripped:
            covers.append(stripped)
    return covers, audited, body


def valid_iso_date(value: str) -> bool:
    try:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)) and bool(date.fromisoformat(value))
    except ValueError:
        return False


def cover_matches(line: str, files: list[str]) -> bool:
    if line.endswith("/"):
        return any(f.startswith(line) for f in files)
    return line in files or any(fnmatch.fnmatchcase(f, line) for f in files)


def _without_fences(body: str) -> str:
    return re.sub(r"```.*?```", "", body, flags=re.S)


def cited_refs(body: str) -> set[tuple[str, str | None]]:
    """(path, symbol or None) for every backticked repository path; line numbers dropped."""
    refs: set[tuple[str, str | None]] = set()
    for chunk in re.findall(r"`([^`\n]+)`", _without_fences(body)):
        for word in chunk.split():
            word = word.strip("\"'(),;")
            if not word.startswith(PATH_PREFIXES):
                continue
            path, _, symbol = word.partition("::")
            path = re.sub(r":\d+(-\d+)?$", "", path).rstrip(".,;)")
            symbol = re.sub(r"\[.*$|\(.*$", "", symbol).rstrip(".,;)")
            if not path or any(c in path for c in "<>{}$…") or "..." in path or path.endswith("#"):
                continue
            if path.startswith(UNTRACKED_PREFIXES):
                continue
            refs.add((path, symbol or None))
    return refs


def _names(nodes: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def symbol_exists(source: str, symbol: str) -> bool:
    """`Symbol`: a top-level class, function or assigned name. `Class.member`: a method or class attribute."""
    tree = ast.parse(source)
    head, _, member = symbol.partition(".")
    if not member:
        return head in _names(tree.body)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == head:
            return member in _names(node.body)
    return False


def _path_exists(path: str, root: Path) -> bool:
    if "*" in path:
        return bool(glob.glob(str(root / path), recursive=True))
    return (root / path).exists()


def check_readme(readme: Path, text: str, root: Path, files: list[str]) -> list[str]:
    """Problems of one headed README; empty when it has no header or everything resolves."""
    parsed = parse_header(text)
    if parsed is None:
        return []
    covers, audited, body = parsed
    name = readme.name
    problems: list[str] = []
    if audited is not None and not valid_iso_date(audited):
        problems.append(f"{name}: header `audited: {audited}` is not a valid YYYY-MM-DD date")
    if not covers:
        problems.append(f"{name}: header has no `covers:` lines")
    for line in covers:
        if not cover_matches(line, files):
            problems.append(f"{name}: covers line {line!r} matches no tracked file")
    for path, symbol in sorted(cited_refs(body), key=lambda r: (r[0], r[1] or "")):
        ref = path if symbol is None else f"{path}::{symbol}"
        if not _path_exists(path, root):
            problems.append(f"{name}: `{ref}` - path does not exist")
            continue
        if symbol is None or not path.endswith(".py") or not (root / path).is_file():
            continue
        if not _SYMBOL.match(symbol):
            continue
        try:
            found = symbol_exists((root / path).read_text(encoding="utf-8"), symbol)
        except SyntaxError as exc:
            problems.append(f"{name}: `{ref}` - {path} does not parse ({exc.msg})")
            continue
        if not found:
            problems.append(f"{name}: `{ref}` - {symbol} is not defined in {path}")
    for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", _without_fences(body)):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue
        rel = target.split("#", 1)[0]
        if rel.endswith(".md") and not (readme.parent / rel).is_file():
            problems.append(f"{name}: link {target!r} - the file does not exist")
    return problems


def block_docs() -> list[Path]:
    """Markdown files of block folders that start with the block header."""
    return [p for p in sorted(BLOCKS_DIR.glob("*/*.md")) if p.read_text(encoding="utf-8").startswith(HEADER)]


# ------------------------------------------------------------------ the check on the tree


@pytest.mark.parametrize("readme", block_docs(), ids=lambda p: p.relative_to(BLOCKS_DIR).as_posix())
def test_block_readme_references_exist(readme):
    problems = check_readme(readme, readme.read_text(encoding="utf-8"), ROOT, tracked_files(ROOT))
    assert not problems, "stale references in a building-block README:\n" + "\n".join(problems)


# ------------------------------------------------------------------ the checker's own tests


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "services").mkdir(parents=True)
    (tmp_path / "src" / "services" / "example_service.py").write_text(
        "LIMIT = 3\n\n\nclass ExampleService:\n    mode = 'x'\n\n    def run(self):\n        pass\n\n"
        "    async def fetch(self):\n        pass\n\n\nasync def start():\n    pass\n"
    )
    (tmp_path / "docs" / "block").mkdir(parents=True)
    (tmp_path / "docs" / "other.md").write_text("# other\n")
    return tmp_path


def _readme(root: Path, covers: str, body: str) -> tuple[Path, str]:
    text = f"<!-- block\ncovers:\n{covers}\n-->\n{body}\n"
    path = root / "docs" / "block" / "README.md"
    path.write_text(text)
    return path, text


def test_parse_header_reads_covers_lines_and_skips_old_format():
    covers, audited, body = parse_header("<!-- block\ncovers:\n  src/a/\n  src/b/*.py\n-->\nbody `x`\n")
    assert covers == ["src/a/", "src/b/*.py"]
    assert audited is None
    assert body.strip() == "body `x`"
    assert parse_header("# Old format\n<!-- block\ncovers:\n a\n-->\n") is None


def test_audited_line_is_parsed_before_or_after_covers_and_must_be_an_iso_date(tmp_path):
    before = parse_header("<!-- block\naudited: 2026-11-20\ncovers:\n  src/a/\n-->\n")
    after = parse_header("<!-- block\ncovers:\n  src/a/\naudited: 2026-11-20\n-->\n")
    assert before is not None and after is not None
    assert before[:2] == after[:2] == (["src/a/"], "2026-11-20")
    assert valid_iso_date("2026-11-20")
    assert not valid_iso_date("2026-13-40") and not valid_iso_date("20 Nov 2026") and not valid_iso_date("")
    root = _repo(tmp_path)
    path, text = _readme(root, "  src/services/\naudited: yesterday", "")
    problems = check_readme(path, text, root, tracked_files(root))
    assert problems == ["README.md: header `audited: yesterday` is not a valid YYYY-MM-DD date"]


def test_cover_lines_match_directory_prefix_glob_and_exact_path():
    files = ["src/services/example_service.py", "src/other.py"]
    assert cover_matches("src/services/", files)
    assert cover_matches("src/services/*_service.py", files)
    assert cover_matches("src/other.py", files)
    assert not cover_matches("src/gone/", files)
    assert not cover_matches("src/gone/*.py", files)
    assert not cover_matches("src/servic", files)


def test_class_member_found_and_missing():
    src = "class ExampleService:\n    def run(self): ...\n    mode = 1\n"
    assert symbol_exists(src, "ExampleService")
    assert symbol_exists(src, "ExampleService.run")
    assert symbol_exists(src, "ExampleService.mode")
    assert not symbol_exists(src, "ExampleService.stop")
    assert not symbol_exists(src, "Other.run")


def test_async_def_and_module_level_assignment_are_symbols():
    src = "LIMIT = 3\nTYPED: int = 1\nasync def start(): ...\nclass A:\n    async def go(self): ...\n"
    assert symbol_exists(src, "start")
    assert symbol_exists(src, "LIMIT") and symbol_exists(src, "TYPED")
    assert symbol_exists(src, "A.go")
    assert not symbol_exists(src, "go")


def test_cited_refs_reads_paths_symbols_and_drops_line_numbers_and_ignored_trees():
    body = "`src/a.py:12` `src/b.py::Cls.run` `src/c.py::fn()` `scripts/memory/x.md` `<src/x>` plain src/d.py\n```\n`src/e.py`\n```"
    assert cited_refs(body) == {("src/a.py", None), ("src/b.py", "Cls.run"), ("src/c.py", "fn")}


def test_a_fully_resolving_readme_has_no_problems(tmp_path):
    root = _repo(tmp_path)
    path, text = _readme(
        root,
        "  src/services/\n  src/services/*_service.py",
        "See `src/services/example_service.py::ExampleService.run`, `src/services/example_service.py::ExampleService.fetch`,"
        " `src/services/example_service.py::start`, `src/services/example_service.py::LIMIT` and [o](../other.md#a).",
    )
    assert check_readme(path, text, root, tracked_files(root)) == []


def test_missing_references_fail_and_name_readme_and_reference(tmp_path):
    root = _repo(tmp_path)
    path, text = _readme(
        root,
        "  src/gone/",
        "`src/services/gone_service.py` `src/services/example_service.py::ExampleService.stop` "
        "`src/services/example_service.py::Missing` [x](../absent.md)",
    )
    problems = check_readme(path, text, root, tracked_files(root))
    joined = "\n".join(problems)
    assert len(problems) == 5, joined
    assert all(p.startswith("README.md:") for p in problems)
    assert "covers line 'src/gone/' matches no tracked file" in joined
    assert "`src/services/gone_service.py` - path does not exist" in joined
    assert "ExampleService.stop is not defined" in joined
    assert "Missing is not defined" in joined
    assert "link '../absent.md'" in joined


def test_non_python_path_needs_existence_only(tmp_path):
    root = _repo(tmp_path)
    path, text = _readme(root, "  docs/other.md", "`docs/other.md::Anything`")
    assert check_readme(path, text, root, tracked_files(root)) == []


def test_readme_without_header_is_skipped(tmp_path):
    root = _repo(tmp_path)
    readme = root / "docs" / "block" / "README.md"
    assert check_readme(readme, "# Old\n`src/gone.py`\n", root, tracked_files(root)) == []
