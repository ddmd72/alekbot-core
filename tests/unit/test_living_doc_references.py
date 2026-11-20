"""Living docs and CLAUDE.md files must not reference code that no longer exists.

Every `docs/**/*.md` that starts with the `<!-- living-doc` header is checked: each `covers:` line matches a tracked
file (or the header says `covers: none`), each backticked repository path exists, each `path::Symbol` /
`path::Class.member` names a real definition (Python files are parsed with `ast`), and relative `.md` links resolve.
No tracked file is covered by two docs. Every tracked `CLAUDE.md` gets the path and symbol checks. A stale reference
misleads the reader the doc exists to orient (docs/04_solution_strategy/decisions/living_docs_contract.md).
Docs without the header are in the old format and are skipped on purpose.
"""
import ast
import fnmatch
import glob
import re
from pathlib import Path

import pytest

from scripts.ci.living_docs import (
    cover_matches,
    covers_conflicts,
    effective_covers,
    living_docs,
    parse_header,
    required_living_docs,
    tracked_files,
    valid_iso_date,
)

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs"

# Real repository trees; a word in backticks starting with one of these is a path.
PATH_PREFIXES = (
    "src/", "tests/", "docs/", "scripts/", "config/", "contracts/", "prompts_snapshot/", "firestore_utils/",
    "docx_generator/", "pdf_generator/", ".github/",
)
# Gitignored trees: they exist on the owner's machine only and are never checked.
UNTRACKED_PREFIXES = ("scripts/memory/", "docs/superpowers/", "firestore_utils/uploads/")

_SYMBOL = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$")


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


def _check_refs(name: str, body: str, root: Path, files: list[str] | None = None) -> list[str]:
    """Problems of the backticked paths and `path::Symbol` refs of `body`.

    With `files` (the tracked list) a glob must match a tracked file; without it, a file on disk.
    """
    problems: list[str] = []
    for path, symbol in sorted(cited_refs(body), key=lambda r: (r[0], r[1] or "")):
        ref = path if symbol is None else f"{path}::{symbol}"
        if files is not None and "*" in path:
            exists = any(fnmatch.fnmatchcase(f, path) for f in files)
        else:
            exists = _path_exists(path, root)
        if not exists:
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
    return problems


def check_readme(readme: Path, text: str, root: Path, files: list[str]) -> list[str]:
    """Problems of one headed doc; empty when it has no header or everything resolves."""
    parsed = parse_header(text)
    if parsed is None:
        return []
    covers, audited, body = parsed
    name = readme.name
    problems: list[str] = []
    if audited is not None and not valid_iso_date(audited):
        problems.append(f"{name}: header `audited: {audited}` is not a valid YYYY-MM-DD date")
    if not covers:
        problems.append(f"{name}: header has no `covers:` lines (an index doc owning no code writes `covers: none`)")
    for line in effective_covers(covers):
        if not cover_matches(line, files):
            problems.append(f"{name}: covers line {line!r} matches no tracked file")
    problems += _check_refs(name, body, root)
    for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", _without_fences(body)):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue
        rel = target.split("#", 1)[0]
        if rel.endswith(".md") and not (readme.parent / rel).is_file():
            problems.append(f"{name}: link {target!r} - the file does not exist")
    return problems


def check_claude_md(rel: str, text: str, root: Path, files: list[str]) -> list[str]:
    """Problems of one CLAUDE.md: a backticked repository path or `path::Symbol` that does not exist."""
    return _check_refs(rel, text, root, files)


def claude_mds() -> list[str]:
    return sorted(f for f in tracked_files(ROOT) if f == "CLAUDE.md" or f.endswith("/CLAUDE.md"))


def doc_headers() -> dict[str, list[str]]:
    docs: dict[str, list[str]] = {}
    for path in living_docs(ROOT):
        parsed = parse_header(path.read_text(encoding="utf-8"))
        if parsed is not None:
            docs[path.relative_to(ROOT).as_posix()] = parsed[0]
    return docs


# ------------------------------------------------------------------ the check on the tree


@pytest.mark.parametrize("readme", living_docs(ROOT), ids=lambda p: p.relative_to(DOCS_DIR).as_posix())
def test_living_doc_references_exist(readme):
    problems = check_readme(readme, readme.read_text(encoding="utf-8"), ROOT, tracked_files(ROOT))
    assert not problems, "stale references in a living doc:\n" + "\n".join(problems)


def test_every_tracked_file_has_at_most_one_owning_doc():
    problems = covers_conflicts(doc_headers(), tracked_files(ROOT))
    assert not problems, "a file covered by two living docs:\n" + "\n".join(problems)


@pytest.mark.parametrize("rel", claude_mds())
def test_claude_md_references_exist(rel):
    problems = check_claude_md(rel, (ROOT / rel).read_text(encoding="utf-8"), ROOT, tracked_files(ROOT))
    assert not problems, "stale references in a CLAUDE.md:\n" + "\n".join(problems)


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
    text = f"<!-- living-doc\ncovers:\n{covers}\n-->\n{body}\n"
    path = root / "docs" / "block" / "README.md"
    path.write_text(text)
    return path, text


def test_parse_header_reads_covers_lines_and_skips_old_format():
    covers, audited, body = parse_header("<!-- living-doc\ncovers:\n  src/a/\n  src/b/*.py\n-->\nbody `x`\n")
    assert covers == ["src/a/", "src/b/*.py"]
    assert audited is None
    assert body.strip() == "body `x`"
    assert parse_header("# Old format\n<!-- living-doc\ncovers:\n a\n-->\n") is None


def test_audited_line_is_parsed_before_or_after_covers_and_must_be_an_iso_date(tmp_path):
    before = parse_header("<!-- living-doc\naudited: 2026-11-20\ncovers:\n  src/a/\n-->\n")
    after = parse_header("<!-- living-doc\ncovers:\n  src/a/\naudited: 2026-11-20\n-->\n")
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


def test_covers_none_is_a_valid_header_with_no_covers(tmp_path):
    root = _repo(tmp_path)
    path, text = _readme(root, "  none", "An index with no code, `docs/other.md`.")
    parsed = parse_header(text)
    assert parsed is not None and parsed[0] == ["none"]
    assert effective_covers(parsed[0]) == []
    assert check_readme(path, text, root, tracked_files(root)) == []
    assert covers_conflicts({"docs/index.md": ["none"], "docs/x.md": ["none"]}, ["none", "src/a/x.py"]) == []


def test_header_without_covers_lines_or_none_is_an_error(tmp_path):
    root = _repo(tmp_path)
    path, text = _readme(root, "", "body")
    problems = check_readme(path, text, root, tracked_files(root))
    assert len(problems) == 1 and "no `covers:` lines" in problems[0]


def test_two_docs_covering_one_file_conflict_and_name_both():
    docs = {"docs/a.md": ["src/a/x.py"], "docs/b.md": ["src/a/*.py"]}
    problems = covers_conflicts(docs, ["src/a/x.py", "src/a/y.py"])
    assert len(problems) == 1
    assert "src/a/x.py" in problems[0] and "docs/a.md" in problems[0] and "docs/b.md" in problems[0]


def test_disjoint_covers_have_no_conflict():
    docs = {"docs/a.md": ["src/a/"], "docs/b.md": ["src/b/*.py", "src/c.py"]}
    assert covers_conflicts(docs, ["src/a/x.py", "src/b/y.py", "src/c.py", "src/d.py"]) == []


def test_directory_cover_and_exact_file_cover_overlap_is_a_conflict():
    docs = {"docs/a.md": ["src/a/"], "docs/b.md": ["src/a/x.py"]}
    problems = covers_conflicts(docs, ["src/a/x.py", "src/a/y.py"])
    assert len(problems) == 1 and "src/a/x.py" in problems[0]


def test_claude_md_missing_path_and_missing_symbol_are_both_reported(tmp_path):
    root = _repo(tmp_path)
    text = "See `src/services/gone_service.py` and `src/services/example_service.py::Missing` and `src/services/example_service.py::start`."
    problems = check_claude_md("src/CLAUDE.md", text, root, tracked_files(root))
    joined = "\n".join(problems)
    assert len(problems) == 2, joined
    assert "`src/services/gone_service.py` - path does not exist" in joined
    assert "Missing is not defined" in joined
    assert all(p.startswith("src/CLAUDE.md:") for p in problems)


def test_claude_md_glob_matching_nothing_is_reported_and_matching_one_is_fine(tmp_path):
    root = _repo(tmp_path)
    files = tracked_files(root)
    assert check_claude_md("CLAUDE.md", "`src/services/*_service.py`", root, files) == []
    problems = check_claude_md("CLAUDE.md", "`src/services/*_gone.py`", root, files)
    assert problems == ["CLAUDE.md: `src/services/*_gone.py` - path does not exist"]


def test_claude_md_ignores_untracked_prefixes(tmp_path):
    root = _repo(tmp_path)
    assert check_claude_md("CLAUDE.md", "`scripts/memory/x.json` `docs/superpowers/y.md`", root, tracked_files(root)) == []


def _without_header(root: Path) -> list[str]:
    return [
        p.relative_to(root).as_posix()
        for p in required_living_docs(root)
        if not p.read_text(encoding="utf-8").startswith("<!-- living-doc")
    ]


def test_every_living_doc_starts_with_the_header():
    missing = _without_header(ROOT)
    assert not missing, f"living docs without the `<!-- living-doc` header: {missing}"


def test_required_living_docs_are_chosen_by_location_and_a_missing_header_is_named(tmp_path):
    header = "<!-- living-doc\ncovers: none\n-->\n"
    files = {
        "docs/README.md": header,
        "docs/05_building_blocks/memory/README.md": "# no header\n",
        "docs/06_runtime/API_REFERENCE.md": header,
        "docs/08_concepts/README.md": header,
        "docs/08_concepts/other_concept.md": "# not required\n",
        "docs/04_solution_strategy/decisions/a_record.md": "# a decision record\n",
        "docs/10_rfcs/SOME_RFC.md": "# an RFC\n",
    }
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    required = [p.relative_to(tmp_path).as_posix() for p in required_living_docs(tmp_path)]
    assert required == [
        "docs/05_building_blocks/memory/README.md",
        "docs/06_runtime/API_REFERENCE.md",
        "docs/08_concepts/README.md",
        "docs/README.md",
    ]
    assert _without_header(tmp_path) == ["docs/05_building_blocks/memory/README.md"]
