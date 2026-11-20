"""scripts/quality/git_units.py: path classes, titles, and the parsers of git's log, diff and blame text.

Every input here is git text written by hand in git's own format; no test in this file runs git (the end-to-end run
over a real throwaway repository is in test_quality_fix_units.py, marked slow).
"""
from datetime import datetime, timezone

import pytest

from scripts.quality.git_units import (
    BlameLine, FileChange, Hunk, Unit, parse_blame, parse_branch_commits, parse_hunks, parse_log, parse_patch_log,
    path_class, unit_title, with_test_defs,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
TS = 1_767_225_600  # 2026-01-01T00:00:00Z


def record(sha: str, parents: str, ts: int, subject: str, body: str = "", changes: str = "") -> str:
    """One commit as `git log -z --raw --numstat --format=<RS>%H<US>%P<US>%ct<US>%s<US>%b<US>` prints it."""
    head = f"\x1e{sha}\x1f{parents}\x1f{ts}\x1f{subject}\x1f{body}\x1f"
    return head + ("\0\n" + changes if changes else "")


def raw(status: str, *paths: str) -> str:
    return f":100644 100644 1111111 2222222 {status}\0" + "".join(p + "\0" for p in paths)


def numstat(added: str, deleted: str, path: str, old: str | None = None) -> str:
    return f"{added}\t{deleted}\t\0{old}\0{path}\0" if old else f"{added}\t{deleted}\t{path}\0"


@pytest.mark.parametrize("path,cls", [
    ("src/services/alpha.py", "src"),
    ("src/alpha.py", "src"),
    ("main.py", "src"),
    ("relay_main.py", "src"),
    ("scripts/beta_main.py", "other"),
    ("src/skills/alpha/SKILL.md", "other"),
    ("tests/unit/services/test_alpha.py", "test"),
    ("tests/conftest.py", "test"),
    ("tests/unit/ledger_alpha.txt", "ledger"),
    ("tests/fixtures/alpha.txt", "other"),
    ("tests/CLAUDE.md", "other"),
    ("docs/alpha.md", "other"),
    ("setup.py", "other"),
])
def test_path_class(path, cls):
    assert path_class(path) == cls


@pytest.mark.parametrize("subject,body,expected", [
    ("Merge pull request #12 from example/alpha", "\nfeat(drive): add a field\n\nmore text\n",
     ("feat(drive): add a field", 12)),
    ("Merge pull request #12 from example/alpha", "", ("Merge pull request #12 from example/alpha", 12)),
    ("fix(queue): keep order (#40)", "", ("fix(queue): keep order (#40)", 40)),
    ("fix(queue): keep order (#40) and more", "", ("fix(queue): keep order (#40) and more", None)),
    ("feat: a | b; c \"d\" (e)", "", ("feat: a | b; c \"d\" (e)", None)),
    ("Merge branch 'alpha'", "", ("Merge branch 'alpha'", None)),
])
def test_unit_title(subject, body, expected):
    assert unit_title(subject, body) == expected


def test_parse_log_merge_and_plain_units():
    text = (
        record(SHA_A, f"{SHA_B} {SHA_C}", TS + 3600, "Merge pull request #12 from example/alpha",
               "feat(drive): add a field\n",
               raw("M", "src/services/alpha.py") + raw("A", "tests/unit/services/test_alpha.py")
               + numstat("3", "1", "src/services/alpha.py") + numstat("20", "0", "tests/unit/services/test_alpha.py"))
        + record(SHA_B, SHA_C, TS, "fix(queue): keep order (#11)", "",
                 raw("D", "src/services/beta.py") + numstat("0", "7", "src/services/beta.py"))
    )
    merge, plain = parse_log(text)
    assert merge == Unit(
        sha=SHA_A, date=datetime(2026, 1, 1, 1, tzinfo=timezone.utc), title="feat(drive): add a field", pr=12,
        files=(FileChange("src/services/alpha.py", "M", 3, 1),
               FileChange("tests/unit/services/test_alpha.py", "A", 20, 0)),
        parents=(SHA_B, SHA_C),
    )
    assert merge.is_merge and not plain.is_merge
    assert (plain.title, plain.pr, plain.files) == (
        "fix(queue): keep order (#11)", 11, (FileChange("src/services/beta.py", "D", 0, 7),))


def test_parse_log_rename_counts_only_changed_lines_and_binary_counts_zero():
    text = record(SHA_A, SHA_B, TS, "refactor: move alpha", "",
                  raw("R087", "tests/unit/test_alpha.py", "tests/unit/services/test_alpha.py")
                  + raw("M", "docs/diagram.png")
                  + numstat("2", "1", "tests/unit/services/test_alpha.py", old="tests/unit/test_alpha.py")
                  + numstat("-", "-", "docs/diagram.png"))
    (unit,) = parse_log(text)
    assert unit.files == (
        FileChange("tests/unit/services/test_alpha.py", "R", 2, 1, old_path="tests/unit/test_alpha.py"),
        FileChange("docs/diagram.png", "M", 0, 0),
    )


def test_parse_log_commit_without_changes_and_title_punctuation():
    (unit,) = parse_log(record(SHA_A, SHA_B, TS, "chore: a\tb | c ; d % e", ""))
    assert unit.files == () and unit.title == "chore: a\tb | c ; d % e"


def test_parse_branch_commits_keeps_subject_and_counts():
    text = record(SHA_A, SHA_B, TS, "test(mechanical): rename a fixture", "body",
                  raw("M", "tests/unit/test_alpha.py") + numstat("4", "4", "tests/unit/test_alpha.py"))
    (commit,) = parse_branch_commits(text)
    assert commit.subject == "test(mechanical): rename a fixture"
    assert commit.files == (FileChange("tests/unit/test_alpha.py", "M", 4, 4),)


DIFF = """diff --git a/src/services/alpha.py b/src/services/alpha.py
index 1111111..2222222 100644
--- a/src/services/alpha.py
+++ b/src/services/alpha.py
@@ -5 +5 @@ def alpha():
-    return 1
+    return 2
@@ -10,0 +11,2 @@ def beta():
+    x = 1
+    return x
@@ -20,3 +21,0 @@ def gamma():
-    a
-    b
-    c
diff --git a/tests/unit/test_alpha.py b/tests/unit/test_alpha.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/unit/test_alpha.py
@@ -0,0 +1,7 @@
+def test_one():
+    pass
+async def test_two():
+    text = "def test_three"
+def helper_test_four():
+  def test_nested():
+--- not a header
diff --git a/tests/unit/test_old.py b/tests/unit/test_old.py
deleted file mode 100644
index 4444444..0000000
--- a/tests/unit/test_old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_gone():
-    pass
diff --git a/src/alpha.py b/src/services/alpha_core.py
similarity index 90%
rename from src/alpha.py
rename to src/services/alpha_core.py
index 5555555..6666666 100644
--- a/src/alpha.py
+++ b/src/services/alpha_core.py
@@ -2 +2 @@
-A = 1
+A = 2
diff --git a/docs/a.md b/docs/b.md
similarity index 100%
rename from docs/a.md
rename to docs/b.md
"""


def test_parse_hunks_old_side_ranges_and_test_defs():
    alpha, new_test, deleted, renamed, pure_rename = parse_hunks(DIFF)
    assert (alpha.path, alpha.old_path) == ("src/services/alpha.py", "src/services/alpha.py")
    assert alpha.hunks == (Hunk(5, 1), Hunk(10, 0), Hunk(20, 3))
    assert (new_test.path, new_test.old_path) == ("tests/unit/test_alpha.py", "")
    # `def test_one`, `async def test_two`, the indented `def test_nested`; not the string, not `helper_test_four`.
    assert (new_test.test_defs_added, new_test.test_defs_removed) == (3, 0)
    assert (deleted.path, deleted.old_path, deleted.test_defs_removed) == ("", "tests/unit/test_old.py", 1)
    assert (renamed.path, renamed.old_path, renamed.hunks) == ("src/services/alpha_core.py", "src/alpha.py", (Hunk(2, 1),))
    assert (pure_rename.path, pure_rename.old_path, pure_rename.hunks) == ("docs/b.md", "docs/a.md", ())


def test_parse_patch_log_splits_by_commit():
    text = f"\x1e{SHA_A}\n\n" + DIFF + f"\x1e{SHA_B}\n"
    out = parse_patch_log(text)
    assert set(out) == {SHA_A, SHA_B}
    assert len(out[SHA_A]) == 5 and out[SHA_B] == []


def test_with_test_defs_fills_matching_files_only():
    unit = Unit(sha=SHA_A, date=datetime(2026, 1, 1, tzinfo=timezone.utc), title="t", pr=None, files=(
        FileChange("tests/unit/test_alpha.py", "A", 7, 0),
        FileChange("tests/unit/test_old.py", "D", 0, 2),
        FileChange("src/services/alpha.py", "M", 4, 4),
    ))
    (filled,) = with_test_defs([unit], parse_patch_log(f"\x1e{SHA_A}\n" + DIFF))
    assert [(f.test_defs_added, f.test_defs_removed) for f in filled.files] == [(3, 0), (0, 1), (0, 0)]


BLAME = f"""{SHA_A} 5 5 2
author Test User
author-mail <test@example.com>
author-time {TS}
author-tz +0000
committer Test User
committer-mail <test@example.com>
committer-time {TS}
committer-tz +0000
summary feat(drive): add a field
filename src/services/alpha.py
\tline five
{SHA_A} 6 6
\tline six
{SHA_B} 9 7 1
author Test User
committer-time {TS + 86400}
boundary
filename src/services/alpha.py
\tline seven
"""


def test_parse_blame_one_entry_per_line_with_committer_time():
    assert parse_blame(BLAME) == [
        BlameLine(SHA_A, TS), BlameLine(SHA_A, TS), BlameLine(SHA_B, TS + 86400),
    ]
