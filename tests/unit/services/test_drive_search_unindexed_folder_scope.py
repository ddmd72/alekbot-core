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
    assert "not indexed" in text and "so this search cannot see them. Browse it." in text
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


async def _repo_with_src():
    w = World()
    projects = w.drive.add("Projects", folder=True)
    root = w.drive.add("alek-site", parent=projects, folder=True)
    w.drive.add(".git", parent=root, folder=True)
    src = w.drive.add("src", parent=root, folder=True)
    w.drive.add("main.py", parent=src)
    await w.settle()
    return w, src


@pytest.mark.parametrize("folder", ["Projects/alek-site/src", "projects/ALEK-SITE/src/"])
async def test_a_path_through_a_hiding_folder_says_not_indexed(folder):
    """The repository matched as a middle segment: its subfolder is real, just not indexed."""
    w, _ = await _repo_with_src()
    result = await w.search("main", folder=folder)
    text = render_results(result)
    assert result.status == STATUS_OK and result.folder_not_indexed and result.cards == ()
    assert "not indexed" in text and "so this search cannot see them. Browse it." in text and "No folder matching" not in text


async def test_a_ref_to_a_folder_inside_a_hiding_folder_says_not_indexed():
    w, src = await _repo_with_src()
    result = await w.search("main", folder=f"drive:{src}")
    assert result.status == STATUS_OK and result.folder_not_indexed
    assert "not indexed" in render_results(result)


async def test_a_ref_to_a_folder_under_a_deleting_ancestor_says_not_indexed():
    from src.domain.drive_index import RecordState
    w = World()
    old = w.drive.add("Old", folder=True)
    sub = w.drive.add("Sub", parent=old, folder=True)
    w.drive.add("x.pdf", parent=sub)
    await w.settle()
    w.set_state(old, state=RecordState.DELETING)
    result = await w.search("x", folder=f"drive:{sub}")
    assert result.status == STATUS_OK and result.folder_not_indexed


async def test_a_path_that_does_not_exist_below_a_normal_folder_is_still_not_found():
    from src.domain.drive_search import STATUS_FOLDER_NOT_FOUND
    w, _ = await _repo_with_src()
    result = await w.search("main", folder="Projects/nowhere")
    assert result.status == STATUS_FOLDER_NOT_FOUND and not result.folder_not_indexed
