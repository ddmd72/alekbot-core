"""H6 spec-status checker (TEST_COVERAGE_SYSTEMIC_RFC §3, H6).

Two checks over `docs/10_rfcs/`:
1. The status the RFC index (`README.md`) shows for an RFC means the same as the RFC's own header, after the
   normalisation below. Headers are free text (`IMPLEMENTED`, `✅ IMPLEMENTED`, `Phases 1–7 Complete`, ...), so the
   comparison is on a small vocabulary, never on strings.
2. An RFC whose status means *implemented* cites only repository paths that exist (in backticks and code blocks). A path
   that was renamed or deleted after the RFC shipped is an exemption with a reason, counted per RFC; the count moves
   both ways, so an RFC that rots further, or gets repaired, fails until its line is updated.

Draft, proposed, accepted (not built), in-progress, superseded, obsolete and rejected RFCs are exempt from check 2: they
may name files that do not exist yet or no longer.

The status vocabulary: draft, proposed, accepted, in-progress, implemented, active, superseded, obsolete, rejected.
Paths under gitignored trees (`scripts/memory/`, `docs/superpowers/`, `firestore_utils/uploads/`) exist on the owner's
machine only and are never checked.
"""
import glob
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RFC_DIR = ROOT / "docs" / "10_rfcs"
VOCABULARY = {"draft", "proposed", "accepted", "in-progress", "implemented", "active", "superseded", "obsolete", "rejected"}

# --- The normalisation table: first matching rule wins; the input is lower-cased, strike-through and markup removed. ---
RULES: list[tuple[str, str]] = [
    (r"^(obsolete|closed)\b", "obsolete"),
    (r"^rejected\b", "rejected"),
    (r"^superseded\b", "superseded"),
    (r"^draft\b", "draft"),
    (r"^proposed\b", "proposed"),
    (r"^(partial|partially)\b", "in-progress"),
    (r"^active\b", "active"),
    (r"^phases?\b.*\bnot built\b", "in-progress"),
    (r"^(implemented|done)\b", "implemented"),
    (r"^phases?\b[^;.]*\b(complete|implemented)\b", "implemented"),
]
# `accepted ...` is decided by what the rest of the line says about the build.
_NOT_BUILT = re.compile(r"\bnot built\b")
_BUILT = re.compile(r"(?<!not )\bbuilt\b|\bimplemented\b")
_IN_PROGRESS = re.compile(r"in progress")
# The build verdict of the whole RFC, as opposed to one slice's ("S1 built", "Built: S1"): it follows the accepted clause.
_VERDICT = re.compile(r"[;,.—-]\s*(built|implemented)\b(?!:)")

# Headers no pattern can classify honestly (a per-revision history, several tracks, a bare step list). The status is
# the owner's reading of the whole header at the time of writing; each line says why it is not a rule.
HEADER_OVERRIDES: dict[str, tuple[str, str]] = {
    "AGENT_SKILLS_RFC.md": ("implemented", "header is a revision history ('Revision 7 ... Delivery A and B shipped'), deliveries A, B, C are built"),
    "DEEP_RESEARCH_RFC.md": ("implemented", "header lists one status per provider track; the Claude runner and the Cloud Run Job are the live ones"),
    "EMAIL_INDEX_V2_RFC.md": ("implemented", "header is a slice list ('Slices 0-10 and 9b built'), every slice is built"),
    "GPT_5_6_MIGRATION_RFC.md": ("superseded", "'Done - shipped, superseded by decisions/gpt_5_6_adoption.md': a finished migration whose record moved to a decision"),
    "USER_DRIVE_RFC.md": ("in-progress", "header is 'Step 1 live ... not yet merged': a step list, not a verdict"),
    "VOICE_MULTI_PROVIDER_RFC.md": ("implemented", "header is a phase list ('A+B merged; C done; D dropped'): everything wanted is done"),
}

# Index rows whose status legitimately differs from the header's after normalisation. Empty is the goal.
INDEX_EXEMPT: dict[str, str] = {}

# Path prefixes that are real repository trees.
PATH_PREFIXES = (
    "src/", "tests/", "docs/", "scripts/", "config/", "contracts/", "prompts_snapshot/", "firestore_utils/",
    "docx_generator/", "pdf_generator/", ".github/",
)
UNTRACKED_PREFIXES = ("scripts/memory/", "docs/superpowers/", "firestore_utils/uploads/")

# Implemented RFCs that cite a path the tree no longer has: `RFC file -> (number of missing paths, reason)`.
# The number must match exactly (fewer: lower it; more: a new rot, fix the RFC).
PATH_EXEMPT: dict[str, tuple[int, str]] = {
    "ACP_V2_SIMPLIFIED_RFC.md": (2, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "AGENT_WORKSPACE_RFC.md": (1, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "DEEP_RESEARCH_RFC.md": (2, "cites files that existed and were deleted or moved since (the OpenAI adapter and webhook of the replaced provider track)"),
    "DELIBERATE_FACT_MANAGEMENT_RFC.md": (14, "lifecycle parts are superseded by FACT_LIFECYCLE_RFC; the migration scripts, v3 service and their tests were planned under names the code never took or lost"),
    "DRIVE_FILE_NOTES_RFC.md": (5, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "DRIVE_SPECIALIST_RFC.md": (2, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "EMAIL_INDEX_V2_RFC.md": (2, "cites files that existed and were deleted or moved since (the email-search profile and token under prompts_snapshot)"),
    "FACT_LIFECYCLE_RFC.md": (1, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "GMAIL_EMAIL_INDEXING_RFC.md": (10, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established; one of the ten (the embedding repair service) existed and was deleted"),
    "GOOGLE_DRIVE_RFC.md": (2, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "HEXAGONAL_PROMPT_CACHING_RFC.md": (1, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "MCP_INFRASTRUCTURE_RFC.md": (1, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "MULTILINGUAL_SUPPORT_RFC.md": (8, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established (the Slack socket adapter, a German locale and test names of the plan)"),
    "PROACTIVE_SELF_REMINDERS_RFC.md": (1, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "PROMPT_TOKEN_SNAPSHOT_RFC.md": (1, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "REMOTE_MCP_SERVER_RFC.md": (1, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "SECRETS_AT_REST_RFC.md": (2, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "TASKS_AGENT_RFC.md": (2, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "TASKS_LOCAL_FIRST_RFC.md": (1, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "TASK_COMPLEXITY_EXECUTION_SETTINGS_RFC.md": (3, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "UNTRUSTED_CONTENT_SANDBOX_RFC.md": (5, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
    "WIDGET_PLAYWRIGHT_RFC.md": (1, "cites files not in the tree; the history here is shallow, so renamed, deleted or planned-only is not established"),
}


def clean_status(text: str) -> str:
    text = re.sub(r"~~.*?~~", " ", text)
    text = re.sub(r"[*`_]|✅|❌|🔄", "", text)
    text = re.sub(r"^[\s\-–—:]+", "", text.strip())
    return re.sub(r"\s+", " ", text).lower()


def normalise(text: str) -> str:
    """One word of the vocabulary for a free-form status line."""
    s = clean_status(text)
    for pattern, status in RULES:
        if re.search(pattern, s):
            return status
    if s.startswith("accepted") or s.startswith("design agreed"):
        built, not_built = bool(_BUILT.search(s)), bool(_NOT_BUILT.search(s))
        if _IN_PROGRESS.search(s) or (built and not_built):
            return "in-progress"
        if not_built:
            return "accepted"
        if _VERDICT.search(s[:400]):
            return "implemented"
        return "in-progress" if built else "accepted"
    return "unknown"


def header_status(text: str) -> str | None:
    """The text after `**Status:**` in the first lines of an RFC (its continuation lines too), or None."""
    lines = text.splitlines()[:40]
    for i, line in enumerate(lines[:12]):
        m = re.match(r"^[-*\s]*\*\*Status:?\*\*:?\s*(.*)$", line)
        if m:
            parts = [m.group(1)]
            for nxt in lines[i + 1:]:
                if not nxt.strip() or nxt.lstrip().startswith(("**", "#", "|", "- **", "---")):
                    break
                parts.append(nxt.strip())
            return " ".join(parts)
    return None


def rfc_status(name: str, text: str) -> str:
    if name in HEADER_OVERRIDES:
        return HEADER_OVERRIDES[name][0]
    raw = header_status(text)
    return "none" if raw is None else normalise(raw)


def index_rows(text: str) -> dict[str, str]:
    """RFC file name -> the status cell of its row in the index table."""
    rows: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*\[[^\]]+\]\(\./([^)]+)\)\s*\|\s*([^|]*?)\s*\|", line)
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def cited_paths(text: str) -> set[str]:
    """Repository paths named inside backticks or fenced code blocks (line numbers and `::test` ids dropped)."""
    chunks = re.findall(r"`([^`\n]+)`", text)
    chunks += [blk for blk in re.findall(r"```[^\n]*\n(.*?)```", text, re.S)]
    paths: set[str] = set()
    for chunk in chunks:
        for word in chunk.split():
            word = word.strip("\"'(),;")
            if not word.startswith(PATH_PREFIXES):
                continue
            word = re.sub(r"(::|:)[\w\[\]\-.,]*$", "", word).rstrip(".,;)")
            if not word or any(c in word for c in "<>{}$…") or "..." in word or word.endswith(("/", "#")):
                continue
            if word.startswith(UNTRACKED_PREFIXES):
                continue
            paths.add(word)
    return paths


def missing_paths(text: str, root: Path) -> list[str]:
    out = []
    for p in sorted(cited_paths(text)):
        found = bool(glob.glob(str(root / p), recursive=True)) if "*" in p else (root / p).exists()
        if not found:
            out.append(p)
    return out


def _rfcs() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(RFC_DIR.glob("*_RFC.md"))}


# ------------------------------------------------------------------ the checks on the tree


def test_every_rfc_header_has_a_status_in_the_vocabulary():
    bad = {n: header_status(t) for n, t in _rfcs().items() if rfc_status(n, t) not in VOCABULARY}
    assert not bad, "status header missing or not classified (add a RULES line or a HEADER_OVERRIDES entry):\n" + "\n".join(
        f"{n}: {s!r}" for n, s in bad.items()
    )


def test_header_overrides_are_needed_and_name_real_rfcs():
    rfcs = _rfcs()
    for name, (status, reason) in HEADER_OVERRIDES.items():
        assert name in rfcs, f"{name} no longer exists"
        assert status in VOCABULARY and len(reason.split()) >= 4, name
        raw = header_status(rfcs[name])
        assert raw is None or normalise(raw) != status, f"{name}: a rule classifies it now ({status}); drop the override"


def test_index_status_means_what_the_header_says():
    rfcs = _rfcs()
    rows = index_rows((RFC_DIR / "README.md").read_text(encoding="utf-8"))
    assert len(rows) >= 40
    mismatches = []
    for name, cell in rows.items():
        assert name in rfcs, f"the index links {name}, which does not exist"
        want = rfc_status(name, rfcs[name])
        got = normalise(cell)
        if got != want and name not in INDEX_EXEMPT:
            mismatches.append(f"{name}: index says {cell!r} ({got}), header says {want}")
    assert not mismatches, "index and header disagree:\n" + "\n".join(mismatches)


def test_index_exemptions_are_still_mismatches():
    rfcs = _rfcs()
    rows = index_rows((RFC_DIR / "README.md").read_text(encoding="utf-8"))
    for name, reason in INDEX_EXEMPT.items():
        assert name in rows and len(reason.split()) >= 4, name
        assert normalise(rows[name]) != rfc_status(name, rfcs[name]), f"{name} matches now; drop the exemption"


def test_implemented_rfcs_cite_only_paths_that_exist():
    problems = []
    for name, text in _rfcs().items():
        if rfc_status(name, text) != "implemented":
            continue
        missing = missing_paths(text, ROOT)
        allowed = PATH_EXEMPT.get(name, (0, ""))[0]
        if len(missing) != allowed:
            problems.append(f"{name}: {len(missing)} missing path(s), exempted {allowed}: {missing}")
    assert not problems, "an implemented RFC cites paths the tree lacks:\n" + "\n".join(problems)


def test_path_exemptions_name_implemented_rfcs_with_a_reason():
    rfcs = _rfcs()
    for name, (count, reason) in PATH_EXEMPT.items():
        assert name in rfcs and rfc_status(name, rfcs[name]) == "implemented", name
        assert count > 0 and len(reason.split()) >= 4, name


# ------------------------------------------------------------------ the checker's own tests


@pytest.mark.parametrize(
    ("header", "status"),
    [
        ("IMPLEMENTED", "implemented"),
        ("✅ IMPLEMENTED", "implemented"),
        ("Implemented (2026-02-21)", "implemented"),
        ("Phases 1–7 Complete", "implemented"),
        ("Phases 1, 1b and 2 implemented; 3–5 not built", "in-progress"),
        ("Partially Implemented (see Implementation Notes)", "in-progress"),
        ("Partial (Routing implemented)", "in-progress"),
        ("OBSOLETE (owner, 2026-11-06) — will not be built", "obsolete"),
        ("CLOSED — not built (owner)", "obsolete"),
        ("~~Proposed~~ **REJECTED — superseded by prompt-based approach**", "rejected"),
        ("SUPERSEDED by X_RFC.md — do not use", "superseded"),
        ("Draft, revision 3.21 — Slice 1 live", "draft"),
        ("Proposed (rev 6); U0–U5 built", "proposed"),
        ("Accepted, not built", "accepted"),
        ("Accepted (owner, 2026-11-06) — not built", "accepted"),
        ("Accepted (owner); step 1 built; step 2 not built", "in-progress"),
        ("Accepted (2026-11-09), implementation in progress", "in-progress"),
        ("Accepted (rev 1), built", "implemented"),
        ("Accepted (owner); implemented on `feat/x`", "implemented"),
        ("Accepted for implementation (owner)", "accepted"),
        ("Active", "active"),
        ("Revision 7 — G1 passed", "unknown"),
    ],
)
def test_normalisation_table(header, status):
    assert normalise(header) == status


def test_header_status_reads_the_first_lines_only():
    assert header_status("# T\n\n**Status:** Implemented  \n") == "Implemented  "
    assert header_status("# T\n- **Status:** Done\n") == "Done"
    assert header_status("# T\n" + "\n" * 20 + "**Status:** late\n") is None


def test_index_rows_reads_name_and_status_cell():
    rows = index_rows("| RFC | Status | D |\n| --- | --- | --- |\n| [A](./A_RFC.md) | Implemented V1 | desc |\n")
    assert rows == {"A_RFC.md": "Implemented V1"}


def test_cited_paths_reads_backticks_and_code_blocks(tmp_path):
    text = (
        "see `src/a.py:12` and `tests/b.py::test_x`, `src/gone/*.py`, `<src/x>`, `scripts/memory/y.md`, `docs/`\n"
        "```\nsrc/c.py docs/d.md\n```\nplain src/not_cited.py\n"
    )
    assert cited_paths(text) == {"src/a.py", "tests/b.py", "src/gone/*.py", "src/c.py", "docs/d.md"}


def test_missing_paths_checks_files_and_globs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    text = "`src/a.py` `src/b.py` `src/*.py` `src/zz/*.py`"
    assert missing_paths(text, tmp_path) == ["src/b.py", "src/zz/*.py"]
