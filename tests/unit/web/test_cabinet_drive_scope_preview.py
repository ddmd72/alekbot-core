"""POST /api/drive/connections/<id>/scope/preview (decisions/drive_scope_add_preview.md): what saving the picker's
scope would newly bring into the index. Only what the saved scope does not already cover is counted; nothing is
written. Over the real services and the fake drive adapter of test_cabinet_drive_connections_2b."""
from tests.unit.web.test_cabinet_drive_connections_2b import USER, World, call


def _path(conn) -> str:
    return f"/api/drive/connections/{conn.connection_id}/scope/preview"


async def _save_scope(w: World, conn, folders, whole=False) -> None:
    capabilities = await w.adapter.capabilities(conn.handle)
    chains = {f: await w.drive.ancestry(conn.handle, f) for f in folders}
    await w.connections.set_scope(USER, conn.connection_id, folder_ids=list(folders), whole_drive=whole,
                                  capabilities=capabilities, write_folder_ancestry=None, folder_ancestries=chains)


def _plant(w: World, conn, parent, *names) -> None:
    for name in names:
        w.adapter.plant(conn.handle, parent, name, b"x")


async def test_a_new_folder_is_counted_by_kind():
    w = World()
    conn = await w.connect(writable=False)
    rd = await w.folder(conn, "R&D")
    _plant(w, conn, rd, "Tom's plan.docx", "lease_porto.pdf", "wa_dentist_ru.jpg", "meeting_2026_09_14.m4a",
           "archive.bin")
    status, body = await call(w.app(), "post", _path(conn), {"whole_drive": False, "folder_ids": [rd]})
    assert status == 200
    assert body == {"new": True, "files": {"documents": 2, "images": 1, "recordings": 1, "other": 1}, "total": 5,
                    "folders": 1, "capped": False, "large": False}


async def test_a_folder_inside_a_saved_one_is_not_new():
    w = World()
    conn = await w.connect(writable=False)
    rd = await w.folder(conn, "R&D")
    inner = await w.folder(conn, "Archive", parent=rd)
    _plant(w, conn, inner, "lease_porto.pdf")
    await _save_scope(w, conn, [rd])
    status, body = await call(w.app(), "post", _path(conn), {"whole_drive": False, "folder_ids": [rd, inner]})
    assert (status, body) == (200, {"new": False})


async def test_only_the_new_folder_is_counted_beside_a_saved_one():
    w = World()
    conn = await w.connect(writable=False)
    rd, inbox = await w.folder(conn, "R&D"), await w.folder(conn, "Inbox")
    _plant(w, conn, rd, "Tom's plan.docx", "lease_porto.pdf")
    _plant(w, conn, inbox, "scan_0012.pdf")
    await _save_scope(w, conn, [rd])
    _, body = await call(w.app(), "post", _path(conn), {"whole_drive": False, "folder_ids": [rd, inbox]})
    assert body["new"] is True and body["total"] == 1 and body["folders"] == 1


async def test_a_requested_parent_and_child_are_counted_once():
    w = World()
    conn = await w.connect(writable=False)
    rd = await w.folder(conn, "R&D")
    inner = await w.folder(conn, "Archive", parent=rd)
    _plant(w, conn, rd, "Tom's plan.docx")
    _plant(w, conn, inner, "lease_porto.pdf")
    _, body = await call(w.app(), "post", _path(conn), {"whole_drive": False, "folder_ids": [inner, rd]})
    assert (body["total"], body["folders"]) == (2, 2)


async def test_whole_drive_on_a_whole_drive_connection_is_not_new():
    w = World()
    conn = await w.connect(writable=False)
    _plant(w, conn, (await w.adapter.get_root(conn.handle)).item_id, "lease_porto.pdf")
    await _save_scope(w, conn, [], whole=True)
    _, body = await call(w.app(), "post", _path(conn), {"whole_drive": True, "folder_ids": []})
    assert body == {"new": False}


async def test_whole_drive_on_a_folder_scope_counts_from_the_root():
    w = World()
    conn = await w.connect(writable=False)
    root = (await w.adapter.get_root(conn.handle)).item_id
    rd = await w.folder(conn, "R&D")
    _plant(w, conn, root, "lease_porto.pdf")
    _plant(w, conn, rd, "Tom's plan.docx")
    _, body = await call(w.app(), "post", _path(conn), {"whole_drive": True, "folder_ids": []})
    assert body["new"] is True and body["total"] == 2 and body["folders"] == 2


async def test_nothing_requested_is_not_new_and_nothing_is_written():
    w = World()
    conn = await w.connect(writable=False)
    _, body = await call(w.app(), "post", _path(conn), {"whole_drive": False, "folder_ids": []})
    assert body == {"new": False}
    assert (await w.connections.get(USER, conn.connection_id)).scope_folder_ids == ()
    assert w.drive_tasks() == []


async def test_a_bad_body_is_400():
    w = World()
    conn = await w.connect(writable=False)
    for bad in ({"folder_ids": []}, {"whole_drive": "yes", "folder_ids": []}, {"whole_drive": False, "folder_ids": "x"},
                {"whole_drive": False, "folder_ids": [""]}, {"whole_drive": False, "folder_ids": [3]}):
        assert (await call(w.app(), "post", _path(conn), bad))[0] == 400, bad


async def test_another_users_connection_is_404_and_the_service_missing_is_501():
    w = World()
    conn = await w.connect(writable=False)
    other = World()
    status, _ = await call(other.app(), "post", _path(conn), {"whole_drive": False, "folder_ids": []})
    assert status == 404
    status, _ = await call(w.app(user_drive_service=None), "post", _path(conn), {"whole_drive": False, "folder_ids": []})
    assert status == 501


async def test_the_route_needs_a_session():
    w = World()
    conn = await w.connect(writable=False)
    app = w.app()
    async with app.test_client() as client:
        resp = await client.post(_path(conn), json={"whole_drive": False, "folder_ids": []})
    assert resp.status_code in (401, 403)
