"""The path rule for files written from outside the bot: the inbox folder under scratch/."""
import pytest

from src.domain.workspace import INBOX_PREFIX, is_outside_written


@pytest.mark.parametrize("path", [
    "scratch/inbox/job/2026-10-20t06-00-00z.md",
    "scratch/inbox/a.md",
    "scratch/inbox/a/b/c/d.md",
])
def test_paths_under_the_inbox_folder_are_outside_written(path):
    assert path.startswith(INBOX_PREFIX)
    assert is_outside_written(path)


@pytest.mark.parametrize("path", [
    "scratch/inboxes/a.md",
    "scratch/inbox.md",
    "scratch/inbox",
    "scratch/inbox/",
    "scratch/a.md",
    "projects/scratch/inbox/a.md",
    "projects/a.md",
    "references/a.md",
    "Scratch/inbox/a.md",
])
def test_lookalikes_and_other_paths_are_not(path):
    assert not is_outside_written(path)
