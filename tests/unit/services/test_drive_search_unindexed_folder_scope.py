"""A folder scope whose contents the index hides (a repository, generated files, a dense tree) says the contents
are not indexed and points to list_files_in_drive, never "No matching files or folders." (USER_DRIVE_INDEXING_RFC
§5.2, §10.1 Folder)."""
import pytest

from src.domain.drive_search import STATUS_OK, render_results

from .test_drive_search_service import World


async def _repo_world():
    w = World()
    projects = w.drive.add("Projects", folder=True)
    root = w.drive.add("alek-site", parent=projects, folder=True)
    w.drive.add(".git", parent=root, folder=True)
    w.drive.add("README.md", parent=root)
    await w.settle()
    return w, root


@pytest.mark.parametrize("query", ["README", ""])
async def test_a_repository_scope_says_its_files_are_not_indexed(query):
    w, _ = await _repo_world()
    result = await w.search(query, folder="Projects/alek-site")
    text = render_results(result)
    assert result.status == STATUS_OK and result.cards == () and result.folder_not_indexed
    assert "not indexed" in text and "list_files_in_drive" in text
    assert "No matching files or folders." not in text and "match the filters" not in text


async def test_the_same_by_ref():
    w, root = await _repo_world()
    result = await w.search("README", folder=f"drive:{root}")
    assert result.folder_not_indexed and "not indexed" in render_results(result)


async def test_a_normal_folder_scope_is_unchanged():
    w = World()
    notes = w.drive.add("Notes", folder=True)
    w.drive.add("plan.md", parent=notes)
    await w.settle()
    result = await w.search("budget", folder="Notes")
    assert not result.folder_not_indexed and "not indexed" not in render_results(result)
