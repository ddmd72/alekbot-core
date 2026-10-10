from src.ports.user_drive_port import UserDrivePort

_EXPECTED = {
    "display_name", "is_connected", "disconnect", "get_root", "get_item", "list_children",
    "search", "download", "upload", "replace_content", "move", "create_folder", "delete",
}


def test_port_is_abstract_with_expected_methods():
    assert _EXPECTED <= set(UserDrivePort.__abstractmethods__)
