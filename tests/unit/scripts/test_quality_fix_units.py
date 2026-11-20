"""scripts/quality/fix_units.py: which units are fixes and the facts printed for each.

The fast tests use hand-built units and blame lines. The `slow` section at the end builds a real throwaway git
repository and runs the git readers and both CLIs (`friction`, `fix_units`) end to end; it is the only place in the
quality tests that runs git.
"""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.quality import fix_units, friction
from scripts.quality.fix_units import (
    blame_ages, default_window, fix_record, introduced_by, is_fix_title, is_fix_unit, layer_of, on_main_ages,
    select_fixes,
)
from scripts.quality.git_units import BlameLine, FileChange, GitParseError, Unit, read_units

AT = datetime(2026, 1, 20, 12, tzinfo=timezone.utc)
DAY = 86400


def unit(n: int, title: str, *files: FileChange, at: datetime = AT, pr: int | None = None) -> Unit:
    return Unit(sha=f"{n:040x}", date=at, title=title, pr=pr, files=files)


@pytest.mark.parametrize("title,is_fix", [
    ("fix: keep order", True),
    ("fix(queue): keep order", True),
    ("fix!: drop the old field", True),
    ("fixture: add a sample", False),
    ("Fix the queue", True),  # case-insensitive, whitespace after `fix`
    ("refactor: fix the queue", False),
    ("fixes: the queue", False),
    (" fix: the queue", False),
])
def test_fix_title(title, is_fix):
    assert is_fix_title(title) is is_fix


def test_a_fix_without_a_src_change_is_not_a_fix_unit():
    assert not is_fix_unit(unit(1, "fix(docs): a link", FileChange("docs/alpha.md", "M", 1, 1),
                                FileChange("tests/unit/test_alpha.py", "M", 1, 1)))
    assert is_fix_unit(unit(2, "fix(queue): keep order", FileChange("relay_main.py", "M", 1, 1)))


def test_select_fixes_keeps_the_oldest_first_and_limits():
    units = [unit(n, "fix(queue): keep order", FileChange("src/services/alpha.py", "M", 1, 0),
                  at=AT - timedelta(days=n)) for n in range(1, 5)]
    units.append(unit(9, "feat(queue): order", FileChange("src/services/alpha.py", "M", 1, 0),
                      at=AT - timedelta(days=30)))
    assert [u.sha for u in select_fixes(units, 2)] == [f"{4:040x}", f"{3:040x}"]
    assert len(select_fixes(units, None)) == 4


@pytest.mark.parametrize("path,layer", [
    ("src/services/alpha.py", "src/services"),
    ("src/adapters/queue/beta.py", "src/adapters"),
    ("src/alpha.py", "src"),
    ("main.py", "entry"),
    ("indexer_main.py", "entry"),
])
def test_layer_of(path, layer):
    assert layer_of(path) == layer


def test_blame_ages_min_is_the_newest_commit_and_add_only_is_null():
    lines = [BlameLine("a" * 40, int(AT.timestamp()) - 3 * DAY), BlameLine("b" * 40, int(AT.timestamp()) - 40 * DAY),
             BlameLine("a" * 40, int(AT.timestamp()) - 3 * DAY)]
    assert blame_ages(lines, AT) == {"min_age_days": 3.0, "max_age_days": 40.0, "blamed_lines": 3}
    assert blame_ages([], AT) == {"min_age_days": None, "max_age_days": None, "blamed_lines": 0}


def test_introduced_by_ranks_units_by_blamed_lines_then_age():
    older = unit(10, "feat(queue): add the queue", at=AT - timedelta(days=50))
    newer = unit(11, "feat(drive): add a field", at=AT - timedelta(days=5))
    oldest = unit(12, "feat(drive): add another field", at=AT - timedelta(days=60))
    fourth = unit(13, "chore: tidy", at=AT - timedelta(days=1))
    # commits c1 and c2 came in with `newer`; c3 with `older`; c4 with `oldest`; c5 with `fourth`
    owner = {"1" * 40: newer, "2" * 40: newer, "3" * 40: older, "4" * 40: oldest, "5" * 40: fourth}
    lines = [BlameLine(s, 0) for s in ["1" * 40, "2" * 40, "3" * 40, "3" * 40, "4" * 40, "4" * 40, "5" * 40]]
    out = introduced_by(lines, owner.__getitem__)
    assert [d["title"] for d in out] == ["feat(drive): add another field", "feat(queue): add the queue",
                                         "feat(drive): add a field"]
    assert out[0] == {"sha": oldest.sha, "date": oldest.date.isoformat(), "title": oldest.title}


def test_fix_record_facts_and_an_add_only_fix():
    u = unit(1, "fix(queue): keep order", FileChange("src/services/alpha.py", "M", 6, 0),
             FileChange("main.py", "M", 2, 0), FileChange("docs/alpha.md", "M", 9, 9),
             FileChange("tests/unit/services/test_alpha.py", "M", 8, 0, test_defs_added=2),
             FileChange("tests/unit/services/test_beta.py", "A", 5, 0, test_defs_added=1), pr=40)
    rec = fix_record(u, [], [])
    assert rec == {
        "sha": u.sha, "date": "2026-01-20T12:00:00+00:00", "pr": 40, "title": "fix(queue): keep order",
        "layers": ["entry", "src/services"], "src_lines_changed": 8,
        "tests_added": 3, "existing_test_files_modified": 1, "has_regression_test": True,
        "fixed_lines_age_days": {"min_age_days": None, "max_age_days": None, "blamed_lines": 0,
                                 "min_age_on_main_days": None, "max_age_on_main_days": None},
        "introduced_by": [],
    }
    assert fix_record(unit(2, "fix: x", FileChange("src/alpha.py", "M", 1, 1)), [], [])["has_regression_test"] is False


# ------------------------------------------------------------------------------------------- slow: a real repository

SRC_PATH = "src/services/alpha.py"
TEST_PATH = "tests/unit/services/test_alpha.py"
LEDGER_PATH = "tests/unit/alpha_allowlist.txt"


def _alpha(**changed: int) -> str:
    values = {k: k for k in range(1, 11)} | {int(k[1:]): v for k, v in changed.items()}
    return "".join(f"VALUE_{k} = {values[k]}\n" for k in range(1, 11))


TEST_V1 = "from src.services import alpha\n\n\ndef test_one():\n    assert alpha.VALUE_1 == 1\n"


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *args: str, at: str | None = None) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test User", "GIT_AUTHOR_EMAIL": "test@example.com",
               "GIT_COMMITTER_NAME": "Test User", "GIT_COMMITTER_EMAIL": "test@example.com",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        if at:
            env |= {"GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at}
        return subprocess.run(["git", *args], cwd=self.root, env=env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def write(self, path: str, text: str) -> None:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, message: str, at: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, at=at)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> dict:
    r = Repo(tmp_path / "origin")
    r.root.mkdir()
    r.git("init", "-q", "-b", "main")
    r.write(SRC_PATH, _alpha())
    r.write(TEST_PATH, TEST_V1)
    r.write(LEDGER_PATH, "alpha\n")
    initial = r.commit("feat(queue): add alpha", "2026-01-05T12:00:00+00:00")
    r.git("checkout", "-q", "-b", "feature")
    r.write(SRC_PATH, _alpha(v3=30))
    r.write(TEST_PATH, TEST_V1 + "\n\ndef test_two():\n    assert alpha.VALUE_3 == 30\n")
    branch = r.commit("feat(drive): change a value", "2026-01-10T12:00:00+00:00")
    r.write(TEST_PATH, (TEST_V1 + "\n\ndef test_two():\n    assert alpha.VALUE_3 == 30\n").replace(
        "import alpha\n", "import alpha  # noqa\n"))
    r.write(LEDGER_PATH, "alpha\nbeta\n")
    r.commit("test(mechanical): mark the import", "2026-01-11T12:00:00+00:00")
    r.git("checkout", "-q", "main")
    r.git("merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #7 from example/feature",
          "-m", "feat(drive): change a value", at="2026-01-13T12:00:00+00:00")
    merge = r.git("rev-parse", "HEAD")
    r.write(SRC_PATH, _alpha(v3=300, v8=80))
    r.write("tests/unit/services/test_beta.py", "async def test_order():\n    pass\n")
    fix = r.commit("fix(queue): keep order (#8)", "2026-01-20T12:00:00+00:00")
    r.git("update-ref", "refs/remotes/origin/main", "HEAD")
    return {"repo": r, "initial": initial, "branch": branch, "merge": merge, "fix": fix}


def _run(main, argv: list[str], capsys) -> tuple[int, str, str]:
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.slow
def test_friction_end_to_end(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo["repo"].root)
    code, out, _ = _run(friction.main, ["--until", "2026-01-26", "--weeks", "3"], capsys)
    assert code == 0
    report = json.loads(out)
    first, merge_week, fix_week = report["weeks"]
    assert [first["week"], merge_week["week"], fix_week["week"]] == ["2026-01-05", "2026-01-12", "2026-01-19"]
    assert (first["src_lines_changed"], first["drag"], first["tests_added"], first["new_test_files"]) == (10, 0.0, 1, 1)
    assert first["new_file_share"] == 1.0
    # the merge is one unit against its first parent: src 1+1, test file 4 added + 1/1, ledger 1 line
    assert (merge_week["units"], merge_week["src_lines_changed"], merge_week["existing_test_lines_changed"]) == (1, 2, 6)
    assert merge_week["drag"] == 3.0
    assert (merge_week["existing_ledger_lines_changed"], merge_week["mechanical_test_lines_changed"]) == (1, 2)
    assert merge_week["bookkeeping_per_kloc"] == 1500.0
    assert merge_week["radius"]["top"] == [
        {"pr": 7, "sha": repo["merge"], "title": "feat(drive): change a value", "files": 1}]
    assert (merge_week["tests_added"], merge_week["new_file_share"]) == (1, 0.0)
    assert (fix_week["src_lines_changed"], fix_week["drag"], fix_week["tests_added_in_new_files"]) == (4, 0.0, 1)
    assert report["hot_test_files"] == [{"path": TEST_PATH, "units": 1}]

    code, out, _ = _run(friction.main, ["--until", "2026-01-26", "--weeks", "3", "--format", "md"], capsys)
    assert code == 0 and out.startswith("# week") and len(out.strip().splitlines()) == 4

    code, out, _ = _run(friction.main, ["--until", "2025-06-02", "--weeks", "2"], capsys)
    assert code == 0 and [w["units"] for w in json.loads(out)["weeks"]] == [0, 0]


@pytest.mark.slow
def test_fix_units_end_to_end(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo["repo"].root)
    code, out, _ = _run(fix_units.main, ["--since", "2026-01-05", "--until", "2026-01-26"], capsys)
    assert code == 0
    (rec,) = json.loads(out)["fix_units"]
    assert (rec["sha"], rec["pr"], rec["title"], rec["layers"]) == (
        repo["fix"], 8, "fix(queue): keep order (#8)", ["src/services"])
    assert (rec["src_lines_changed"], rec["tests_added"], rec["existing_test_files_modified"]) == (4, 1, 0)
    assert rec["has_regression_test"] is True
    # line 3 came from the branch commit (10 days before the fix), line 8 from the initial commit (15 days)
    # on main: line 3 since the merge (7 days before the fix), line 8 on the first-parent chain itself (15 days)
    assert rec["fixed_lines_age_days"] == {"min_age_days": 10.0, "max_age_days": 15.0, "blamed_lines": 2,
                                           "min_age_on_main_days": 7.0, "max_age_on_main_days": 15.0}
    # one line each: the older unit first; the branch commit maps to the merge that brought it in
    assert [(d["sha"], d["title"]) for d in rec["introduced_by"]] == [
        (repo["initial"], "feat(queue): add alpha"), (repo["merge"], "feat(drive): change a value")]

    code, out, _ = _run(fix_units.main, ["--since", "2026-01-21", "--until", "2026-01-26"], capsys)
    assert code == 0 and json.loads(out)["fix_units"] == []


@pytest.mark.slow
def test_unknown_ref_and_shallow_history_exit_2(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(repo["repo"].root)
    code, out, err = _run(friction.main, ["--ref", "origin/nope", "--until", "2026-01-26"], capsys)
    assert (code, out) == (2, "") and "unknown ref" in err and len(err.strip().splitlines()) == 1
    code, _, err = _run(fix_units.main, ["--ref", "origin/nope"], capsys)
    assert code == 2 and "unknown ref" in err

    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo['repo'].root}", str(shallow)], check=True,
                   capture_output=True)
    monkeypatch.chdir(shallow)
    code, out, err = _run(friction.main, ["--until", "2026-01-26", "--weeks", "3"], capsys)
    assert (code, out) == (2, "") and "shallow" in err
    code, _, err = _run(fix_units.main, ["--since", "2026-01-05", "--until", "2026-01-26"], capsys)
    assert code == 2 and "shallow" in err
    # a window after the oldest reachable commit is refused too: blame would stop at the shallow boundary
    code, out, err = _run(fix_units.main, ["--since", "2026-01-21", "--until", "2026-01-26"], capsys)
    assert (code, out) == (2, "") and "shallow" in err and len(err.strip().splitlines()) == 1


# ------------------------------------------------------------------------------------------- review fixes (fast)


@pytest.mark.parametrize("title,is_fix", [
    ("Fix search: keep the order", True),
    ("fix the queue order", True),
    ("FIX(queue): keep order", True),
    ("fix\tthe queue", True),
    ("fixture: add a sample", False),
    ("fixes the queue", False),
    ("refactor: fix the queue", False),
    ("hotfix: the queue", False),
    ("fix", False),
])
def test_fix_title_is_case_insensitive_and_takes_whitespace(title, is_fix):
    assert is_fix_title(title) is is_fix


def test_age_on_main_counts_from_the_unit_that_brought_the_commit_in():
    merged = unit(20, "feat(drive): add a field", at=AT - timedelta(days=1))  # the branch line landed a day ago
    on_chain = unit(21, "feat(queue): add the queue", at=AT - timedelta(days=30))
    owner = {"1" * 40: merged, "2" * 40: on_chain}
    # commit 1 was written ten days ago on the branch; commit 2 is itself on the first-parent chain
    lines = [BlameLine("1" * 40, int(AT.timestamp()) - 10 * DAY), BlameLine("2" * 40, int(AT.timestamp()) - 30 * DAY)]
    assert on_main_ages(lines, AT, owner.__getitem__) == {"min_age_on_main_days": 1.0, "max_age_on_main_days": 30.0}
    assert blame_ages(lines, AT)["min_age_days"] == 10.0
    assert on_main_ages([], AT, owner.__getitem__) == {"min_age_on_main_days": None, "max_age_on_main_days": None}


def test_fix_record_carries_the_on_main_ages():
    u = unit(1, "fix(queue): keep order", FileChange("src/services/alpha.py", "M", 1, 1))
    lines = [BlameLine("1" * 40, int(AT.timestamp()) - 4 * DAY)]
    on_main = {"min_age_on_main_days": 2.0, "max_age_on_main_days": 2.0}
    assert fix_record(u, lines, [], on_main)["fixed_lines_age_days"] == {
        "min_age_days": 4.0, "max_age_days": 4.0, "blamed_lines": 1,
        "min_age_on_main_days": 2.0, "max_age_on_main_days": 2.0,
    }
    assert fix_record(u, [], [])["fixed_lines_age_days"]["min_age_on_main_days"] is None


@pytest.mark.parametrize("today,window", [
    (datetime(2026, 1, 21).date(), ("2026-01-12", "2026-01-19")),  # a Wednesday
    (datetime(2026, 1, 19).date(), ("2026-01-12", "2026-01-19")),  # a Monday: the week that ended that day
    (datetime(2026, 1, 18).date(), ("2026-01-05", "2026-01-12")),  # a Sunday: its week has not ended
])
def test_default_window_is_the_last_complete_week(today, window):
    since, until = default_window(today)
    assert (since.isoformat(), until.isoformat()) == window


def test_unreadable_git_text_exits_2_with_one_line(monkeypatch, capsys):
    def unreadable(*args, **kwargs):
        raise GitParseError("blamed commit without a committer-time")

    monkeypatch.setattr(fix_units, "read_units", unreadable)
    assert fix_units.main(["--since", "2026-01-05", "--until", "2026-01-12"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and len(out.err.strip().splitlines()) == 1 and "committer-time" in out.err


# ------------------------------------------------------------------------------------------- review fixes (slow)


@pytest.mark.slow
def test_age_on_main_end_to_end(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo["repo"].root)
    code, out, _ = _run(fix_units.main, ["--since", "2026-01-19", "--until", "2026-01-26"], capsys)
    assert code == 0
    (rec,) = json.loads(out)["fix_units"]
    ages = rec["fixed_lines_age_days"]
    # line 3: written on the branch 10 days before the fix, on main since the merge 7 days before it;
    # line 8: from the initial commit, on the first-parent chain itself, 15 days in both counts
    assert (ages["min_age_days"], ages["max_age_days"]) == (10.0, 15.0)
    assert (ages["min_age_on_main_days"], ages["max_age_on_main_days"]) == (7.0, 15.0)


@pytest.mark.slow
def test_a_shallow_clone_exits_2_even_when_it_covers_the_window(repo, tmp_path, monkeypatch, capsys):
    shallow = tmp_path / "shallow3"
    subprocess.run(["git", "clone", "-q", "--depth", "3", f"file://{repo['repo'].root}", str(shallow)], check=True,
                   capture_output=True)
    monkeypatch.chdir(shallow)
    for main, argv in ((friction.main, ["--until", "2026-01-26", "--weeks", "1"]),
                       (fix_units.main, ["--since", "2026-01-21", "--until", "2026-01-26"])):
        code, out, err = _run(main, argv, capsys)
        assert (code, out) == (2, "")
        assert "git fetch --unshallow" in err and len(err.strip().splitlines()) == 1


def _base_repo(root: Path) -> Repo:
    r = Repo(root)
    r.root.mkdir()
    r.git("init", "-q", "-b", "main")
    r.write(SRC_PATH, _alpha())
    r.write("src/services/beta.py", "BETA = 1\n")
    r.write(TEST_PATH, TEST_V1)
    r.write("tests/unit/services/test_beta.py", "def test_beta():\n    assert True\n")
    r.commit("feat(queue): add alpha and beta", "2026-01-05T12:00:00+00:00")
    return r


@pytest.mark.slow
def test_a_merge_unit_is_its_diff_against_the_first_parent_when_main_moved_on(tmp_path):
    r = _base_repo(tmp_path / "moved")
    r.git("checkout", "-q", "-b", "feature")
    r.write(SRC_PATH, _alpha(v3=30))
    r.commit("feat(drive): change a value", "2026-01-07T12:00:00+00:00")
    r.git("checkout", "-q", "main")
    r.write("src/services/beta.py", "BETA = 2\nGAMMA = 3\n")
    r.commit("feat(queue): change beta", "2026-01-08T12:00:00+00:00")  # main advances after the fork point
    r.git("merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #9 from example/feature",
          "-m", "feat(drive): change a value", at="2026-01-13T12:00:00+00:00")
    units = read_units("main", datetime(2026, 1, 12, tzinfo=timezone.utc), datetime(2026, 1, 19, tzinfo=timezone.utc),
                       cwd=str(r.root))
    (merge,) = units
    assert merge.pr == 9
    assert [(f.path, f.status, f.added, f.deleted) for f in merge.files] == [(SRC_PATH, "M", 1, 1)]


@pytest.mark.slow
def test_a_branch_that_merged_main_keeps_only_its_own_commits(tmp_path, monkeypatch, capsys):
    r = _base_repo(tmp_path / "synced")
    r.git("checkout", "-q", "-b", "feature")
    r.write(SRC_PATH, _alpha(v3=30))
    r.write(TEST_PATH, TEST_V1 + "\n\ndef test_two():\n    assert alpha.VALUE_3 == 30\n")
    r.commit("feat(drive): change a value", "2026-01-06T12:00:00+00:00")
    r.git("checkout", "-q", "main")
    r.write("tests/unit/services/test_beta.py", "def test_beta():\n    assert 1\n")
    r.write("src/services/beta.py", "BETA = 2\n")
    r.commit("test(mechanical): another change on main", "2026-01-07T12:00:00+00:00")
    r.git("checkout", "-q", "feature")
    r.git("merge", "-q", "--no-ff", "main", "-m", "Merge branch 'main' into feature", at="2026-01-08T12:00:00+00:00")
    r.write(TEST_PATH, (TEST_V1 + "\n\ndef test_two():\n    assert alpha.VALUE_3 == 30\n").replace(
        "import alpha\n", "import alpha  # noqa\n"))
    r.commit("test(mechanical): mark the import", "2026-01-09T12:00:00+00:00")
    r.git("checkout", "-q", "main")
    r.git("merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #10 from example/feature",
          "-m", "feat(drive): change a value", at="2026-01-13T12:00:00+00:00")
    r.git("update-ref", "refs/remotes/origin/main", "HEAD")
    units = read_units("main", datetime(2026, 1, 12, tzinfo=timezone.utc), datetime(2026, 1, 19, tzinfo=timezone.utc),
                       cwd=str(r.root))
    (merge,) = units
    assert sorted(c.subject for c in merge.branch_commits) == [
        "feat(drive): change a value", "test(mechanical): mark the import"]
    assert sorted(f.path for f in merge.files) == [SRC_PATH, TEST_PATH]
    monkeypatch.chdir(r.root)
    code, out, _ = _run(friction.main, ["--until", "2026-01-19", "--weeks", "1"], capsys)
    (week,) = json.loads(out)["weeks"]
    assert code == 0 and week["mechanical_test_lines_changed"] == 2  # main's mechanical commit is not the branch's


@pytest.mark.slow
def test_the_user_git_config_does_not_change_the_numbers(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo["repo"].root)
    argv = ["--since", "2026-01-19", "--until", "2026-01-26"]
    _, plain, _ = _run(fix_units.main, argv, capsys)
    config = repo["repo"].root.parent / "gitconfig"
    config.write_text("[color]\n\tui = always\n[log]\n\tshowSignature = true\n[diff]\n\tnoprefix = true\n"
                          "\trenameLimit = 1\n[core]\n\tquotePath = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    code, configured, _ = _run(fix_units.main, argv, capsys)
    assert code == 0 and configured == plain
    code, out, _ = _run(friction.main, ["--until", "2026-01-26", "--weeks", "3"], capsys)
    assert code == 0 and json.loads(out)["weeks"][1]["drag"] == 3.0
