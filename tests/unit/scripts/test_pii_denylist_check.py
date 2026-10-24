"""scripts/pii_gate/denylist_check.py: added lines against a private `tier regex` denylist, never echoing a match.

Every marker here is invented (`zorblax`, `quuxville`, `plimbo`); the real denylist never enters the repository.
"""

import subprocess

import pytest

from scripts.pii_gate import added_lines, denylist_check

DENYLIST = "# invented test denylist\n\nhard \\bzorblax\\b\nsoft quuxville\nhard plimbo-\\d{3}\n"
HARD_LINE, SOFT_LINE, HARD2_LINE = 3, 4, 5        # each rule's line number in DENYLIST


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch, tmp_path):
    for name in ("PII_DENYLIST", "PII_DENYLIST_FILE"):
        monkeypatch.delenv(name, raising=False)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Alex Doe\n\temail = alex@example.com\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "notes.md").write_text("first line\nsecond line\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def denylist(tmp_path):
    path = tmp_path / "denylist.txt"
    path.write_text(DENYLIST)
    return str(path)


def run(capsys, *argv):
    code = denylist_check.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


class TestStaged:
    def test_a_hard_hit_blocks_and_names_file_line_tier_and_rule(self, repo, denylist, capsys):
        (repo / "notes.md").write_text("first line\nsecond line\nmy cousin Zorblax visits\n")
        git(repo, "add", "notes.md")
        code, out, _ = run(capsys, "--staged", "--denylist", denylist)
        assert code == 1
        assert f"notes.md:3 hard {HARD_LINE}" in out.splitlines()

    def test_a_soft_hit_warns_and_passes(self, repo, denylist, capsys):
        (repo / "notes.md").write_text("first line\nsecond line\nmoved to quuxville\n")
        git(repo, "add", "notes.md")
        code, out, err = run(capsys, "--staged", "--denylist", denylist)
        assert code == 0
        assert f"notes.md:3 soft {SOFT_LINE}" in out.splitlines()
        assert "warning" in err

    def test_the_matched_text_and_the_pattern_never_appear_in_the_output(self, repo, denylist, capsys):
        (repo / "notes.md").write_text("first line\nsecond line\nZORBLAX in quuxville, badge plimbo-123\n")
        git(repo, "add", "notes.md")
        code, out, err = run(capsys, "--staged", "--denylist", denylist)
        assert code == 1
        everything = (out + err).lower()
        for leaked in ("zorblax", "quuxville", "plimbo", "badge", "\\d{3}"):
            assert leaked not in everything
        assert {f"notes.md:3 hard {HARD_LINE}", f"notes.md:3 soft {SOFT_LINE}",
                f"notes.md:3 hard {HARD2_LINE}"} <= set(out.splitlines())

    def test_only_added_lines_are_checked_a_removed_marker_passes(self, repo, denylist, capsys):
        (repo / "notes.md").write_text("first line\nzorblax was here\n")
        git(repo, "commit", "-qam", "add a marker")
        (repo / "notes.md").write_text("first line\nnothing private\n")
        git(repo, "add", "notes.md")
        code, out, _ = run(capsys, "--staged", "--denylist", denylist)
        assert code == 0
        assert "hard" not in out

    def test_an_unstaged_marker_is_not_checked(self, repo, denylist, capsys):
        (repo / "notes.md").write_text("first line\nsecond line\nzorblax\n")
        code, _, _ = run(capsys, "--staged", "--denylist", denylist)
        assert code == 0

    def test_a_path_that_matches_is_reported_without_the_path(self, repo, denylist, capsys):
        (repo / "zorblax-family.md").write_text("an ordinary line\nquuxville\n")
        git(repo, "add", ".")
        code, out, err = run(capsys, "--staged", "--denylist", denylist)
        assert code == 1
        assert "zorblax" not in (out + err).lower()
        assert f"(file path #1 in the change) hard {HARD_LINE}" in out.splitlines()
        assert f"(file path #1 in the change):2 soft {SOFT_LINE}" in out.splitlines()

    def test_lockfiles_and_the_gazetteer_are_skipped(self, repo, denylist, capsys):
        (repo / "src" / "assets").mkdir(parents=True)
        (repo / "src" / "assets" / "places.tsv").write_text("quuxville\tzorblax\n")
        (repo / "package-lock.json").write_text('{"integrity": "sha512-zorblax"}\n')
        git(repo, "add", ".")
        code, out, _ = run(capsys, "--staged", "--denylist", denylist)
        assert code == 0
        assert "hard" not in out and "soft" not in out


class TestConfig:
    def test_no_denylist_prints_one_notice_and_passes(self, repo, capsys):
        (repo / "notes.md").write_text("zorblax\n")
        git(repo, "add", "notes.md")
        code, out, err = run(capsys, "--staged")
        assert code == 0
        assert out.strip() == denylist_check.NO_CONFIG_NOTICE
        assert err == ""

    def test_require_turns_a_missing_denylist_into_an_error(self, repo, capsys):
        code, _, err = run(capsys, "--staged", "--require")
        assert code == 2
        assert "no rules" in err

    def test_require_refuses_a_denylist_of_comments_only(self, repo, monkeypatch, capsys):
        monkeypatch.setenv("PII_DENYLIST", "# nothing\n\n")
        code, _, _ = run(capsys, "--staged", "--require")
        assert code == 2

    def test_the_content_variable_is_read(self, repo, monkeypatch, capsys):
        monkeypatch.setenv("PII_DENYLIST", DENYLIST)
        (repo / "notes.md").write_text("zorblax\n")
        git(repo, "add", "notes.md")
        code, out, _ = run(capsys, "--staged")
        assert code == 1 and f"notes.md:1 hard {HARD_LINE}" in out

    def test_the_file_variable_wins_over_the_content_variable(self, repo, denylist, monkeypatch, capsys):
        monkeypatch.setenv("PII_DENYLIST_FILE", denylist)
        monkeypatch.setenv("PII_DENYLIST", "hard ordinary\n")
        (repo / "notes.md").write_text("an ordinary line\n")
        git(repo, "add", "notes.md")
        code, _, _ = run(capsys, "--staged")
        assert code == 0

    def test_an_unreadable_denylist_file_is_an_error(self, repo, tmp_path, capsys):
        code, _, err = run(capsys, "--staged", "--denylist", str(tmp_path / "missing.txt"))
        assert code == 2 and "unreadable" in err

    @pytest.mark.parametrize("bad, reason", [("hard [zorblax", "invalid regex"), ("medium zorblax", "expected")])
    def test_a_malformed_rule_is_an_error_that_names_the_line_not_the_pattern(self, repo, monkeypatch, capsys,
                                                                                 bad, reason):
        monkeypatch.setenv("PII_DENYLIST", f"# x\n{bad}\n")
        code, out, err = run(capsys, "--staged")
        assert code == 2
        assert "line 2" in err and reason in err
        assert "zorblax" not in (out + err)


class TestOtherSources:
    def test_a_range_checks_added_lines_and_commit_messages(self, repo, denylist, capsys):
        base = git(repo, "rev-parse", "HEAD").strip()
        (repo / "notes.md").write_text("first line\nsecond line\nthird line\n")
        git(repo, "commit", "-qam", "tidy notes\n\nmentions quuxville in the body")
        sha = git(repo, "rev-parse", "HEAD").strip()
        code, out, err = run(capsys, "--range", f"{base}...HEAD", "--denylist", denylist)
        assert code == 0
        assert f"commit {sha[:12]}:3 soft {SOFT_LINE}" in out.splitlines()
        assert "quuxville" not in (out + err)

    def test_files_mode_reads_every_line_and_skips_binary_files(self, tmp_path, denylist, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.txt").write_text("one\nzorblax\n")
        (tmp_path / "b.bin").write_bytes(b"\x00\x01zorblax")
        code, out, _ = run(capsys, "--files", "a.txt", "b.bin", "--denylist", denylist)
        assert code == 1
        assert out.splitlines()[0] == f"a.txt:2 hard {HARD_LINE}"
        assert "b.bin" not in out

    def test_message_mode_skips_git_comment_lines(self, tmp_path, denylist, capsys):
        msg = tmp_path / "COMMIT_EDITMSG"
        msg.write_text("feat: thing\n# zorblax in a git comment\nbody plimbo-123\n")
        code, out, _ = run(capsys, "--message", str(msg), "--denylist", denylist)
        assert code == 1
        assert out.splitlines()[0] == f"commit message:3 hard {HARD2_LINE}"


class TestParseUnifiedDiff:
    def test_line_numbers_follow_the_new_side_and_removed_lines_are_dropped(self):
        diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                "@@ -2,2 +2,3 @@\n-old one\n-old two\n+new two\n+++plus-prefixed content\n+new four\n"
                "@@ -10 +11 @@\n-gone\n+eleven\n\\ No newline at end of file\n")
        change = added_lines.parse_unified_diff(diff)
        assert [(x.line, x.text) for x in change.lines] == [
            (2, "new two"), (3, "++plus-prefixed content"), (4, "new four"), (11, "eleven")]
        assert change.paths == ["x.py"]

    def test_deleted_and_binary_files_yield_nothing(self):
        diff = ("diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n--- a/gone.txt\n+++ /dev/null\n"
                "@@ -1 +0,0 @@\n-zorblax\n"
                "diff --git a/img.png b/img.png\nnew file mode 100644\nBinary files /dev/null and b/img.png differ\n")
        change = added_lines.parse_unified_diff(diff)
        assert change.lines == [] and change.paths == []

    def test_a_quoted_path_is_unquoted_and_a_rename_target_is_a_path(self):
        diff = ('diff --git "a/odd\\tname.md" "b/odd\\tname.md"\n--- "a/odd\\tname.md"\n+++ "b/odd\\tname.md"\n'
                "@@ -0,0 +1 @@\n+hello\n"
                "diff --git a/old.md b/new.md\nsimilarity index 100%\nrename from old.md\nrename to new.md\n")
        change = added_lines.parse_unified_diff(diff)
        assert change.lines == [added_lines.AddedLine("odd\tname.md", 1, "hello")]
        assert change.paths == ["odd\tname.md", "new.md"]
