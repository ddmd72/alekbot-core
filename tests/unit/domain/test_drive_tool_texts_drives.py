"""The tool declarations carry `drives` where RFC §11.2 puts it, and the list text names the tree root."""
from src.domain import drive_tool_texts as tt


def _params(name):
    tool = next(d for d in tt.drive_tool_declarations() if d["name"] == name)
    return list(tool["parameters"]["properties"])


def test_search_and_passages_declare_drives_after_folder():
    search = _params(tt.SEARCH_DRIVE)
    assert search.index("drives") == search.index("folder") + 1
    passages = _params(tt.FIND_PASSAGES)
    assert passages.index("drives") == passages.index("folder") + 1


def test_no_other_tool_declares_drives():
    others = [d["name"] for d in tt.drive_tool_declarations() if d["name"] not in (tt.SEARCH_DRIVE, tt.FIND_PASSAGES)]
    assert others and all("drives" not in _params(name) for name in others)
    assert "drives" not in _params(tt.READ_PASSAGES) and "drives" not in _params(tt.OPEN_DRIVE_FILE)


def test_the_drives_text_names_one_drive_and_the_primary_default():
    assert "one drive" in tt.DRIVES and "primary" in tt.DRIVES and "drive_state" in tt.DRIVES


def test_the_file_refs_text_says_drives_is_not_used_with_them():
    assert "folder and drives are not used" in tt.PASSAGES_FILE_REFS


def test_the_list_folder_text_names_the_tree_root():
    assert "'/' lists the drives" in tt.LIST_FOLDER_PARAM and "primary drive's top level" in tt.LIST_FOLDER_PARAM
    assert "folder path" in tt._FOLDER and "drive: ref" in tt._FOLDER
