"""DriveProviderRouter and the registry's drive adapters (MULTI_DRIVE_RFC §6, T-4 stage-1 router part, T-8).

The router resolves every call by `handle.kind` to the adapter registered for it, passing the handle and every argument
through unchanged; two connections of one kind and one of another keep their own items; an unknown kind is an unusable
connection (DriveNotConnectedError). T-8: every routed method body is one dispatch call.
"""
import ast
import inspect
import textwrap
from datetime import datetime, timezone
from typing import Dict, Tuple

import pytest

from src.composition.connector_registry import ConnectorRegistry
from src.composition.drive_provider_router import DriveProviderRouter
from src.domain.connector_spec import ConnectorSpec
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_connection import DriveConnection
from src.domain.drive_handle import DriveHandle
from src.domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from src.domain.drive_media_source import MediaSource
from src.domain.integration import Integration
from src.domain.user_drive import DriveItem, DriveItemNotFoundError, DriveNameConflictError, DriveNotConnectedError
from src.ports.user_drive_port import UserDrivePort

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SINGLE_DRIVE_MEMBERS = {"display_name", "is_connected", "disconnect", "connection", "flag_reconnect", "download_url"}


class _MemDrive(UserDrivePort):
    """An in-memory drive per connection: items keyed by (connection_id, item_id); a root per connection."""

    def __init__(self, kind: str, max_path_chars: int) -> None:
        self.kind = kind
        self._max = max_path_chars
        self._items: Dict[Tuple[str, str], DriveItem] = {}
        self._data: Dict[Tuple[str, str], bytes] = {}
        self._parent: Dict[Tuple[str, str], str] = {}
        self._next = 0

    def _check(self, handle: DriveHandle) -> None:
        assert handle.kind == self.kind, "routed to the wrong adapter"

    def _root(self, handle: DriveHandle) -> DriveItem:
        key = (handle.connection_id, "root")
        if key not in self._items:
            self._items[key] = DriveItem(item_id="root", name="", path="", is_folder=True,
                                         identity=f"{self.kind}:{handle.connection_id}:root")
        return self._items[key]

    def _get(self, handle: DriveHandle, item_id: str) -> DriveItem:
        self._check(handle)
        self._root(handle)
        try:
            return self._items[(handle.connection_id, item_id)]
        except KeyError:
            raise DriveItemNotFoundError(item_id) from None

    async def get_root(self, handle):
        self._check(handle)
        return self._root(handle)

    async def get_item(self, handle, item_id):
        return self._get(handle, item_id)

    async def list_children(self, handle, folder_id):
        self._get(handle, folder_id)
        return [self._items[k] for k, parent in self._parent.items()
                if k[0] == handle.connection_id and parent == folder_id]

    async def search(self, handle, query, limit):
        self._check(handle)
        return [i for (c, _), i in self._items.items() if c == handle.connection_id and query in i.name][:limit]

    async def download(self, handle, item_id):
        self._get(handle, item_id)
        return self._data[(handle.connection_id, item_id)]

    async def upload(self, handle, parent_id, filename, data, content_type, conflict="rename"):
        parent = self._get(handle, parent_id)
        if await self.child_by_name(handle, parent_id, filename) is not None and conflict == "fail":
            raise DriveNameConflictError(filename)
        self._next += 1
        item_id = f"item-{self._next}"
        item = DriveItem(item_id=item_id, name=filename, path=f"{parent.path}/{filename}".lstrip("/"),
                         is_folder=False, size_bytes=len(data), mime_type=content_type,
                         identity=f"{self.kind}:{handle.connection_id}:{item_id}")
        self._items[(handle.connection_id, item_id)] = item
        self._data[(handle.connection_id, item_id)] = data
        self._parent[(handle.connection_id, item_id)] = parent_id
        return item

    async def replace_content(self, handle, item_id, data, content_type):
        self._get(handle, item_id)
        self._data[(handle.connection_id, item_id)] = data
        return self._items[(handle.connection_id, item_id)]

    async def move(self, handle, item_id, new_parent_id=None, new_name=None):
        item = self._get(handle, item_id)
        if new_parent_id:
            self._parent[(handle.connection_id, item_id)] = new_parent_id
        return item

    async def create_folder(self, handle, parent_id, name):
        return await self.upload(handle, parent_id, name, b"", "folder")

    async def delete(self, handle, item_id):
        self._get(handle, item_id)
        del self._items[(handle.connection_id, item_id)]

    async def child_by_name(self, handle, parent_id, name):
        self._check(handle)
        for (c, item_id), parent in self._parent.items():
            if c == handle.connection_id and parent == parent_id and self._items[(c, item_id)].name == name:
                return DriveChange(item_id=item_id, name=name, parent_id=parent_id)
        return None

    async def area_path(self, handle):
        self._check(handle)
        return f"/{self.kind}"

    async def get_changes(self, handle, cursor):
        self._check(handle)
        changes = tuple(DriveChange(item_id=i.item_id, name=i.name) for (c, _), i in self._items.items()
                        if c == handle.connection_id)
        return DriveChanges(changes=changes, cursor="end", complete=True, full_enumeration=cursor is None,
                            drive_id=handle.connection_id, root_item_id="root",
                            drive_identity=f"{self.kind}:{handle.connection_id}")

    async def get_change(self, handle, item_id):
        item = self._get(handle, item_id)
        return DriveChange(item_id=item.item_id, name=item.name, identity=item.identity)

    async def list_children_changes(self, handle, folder_id, limit, cursor=None):
        children = await self.list_children(handle, folder_id)
        return DriveChildrenPage(changes=tuple(DriveChange(item_id=i.item_id) for i in children[:limit]))

    async def download_to_file(self, handle, item_id, dest_path, max_bytes):
        data = await self.download(handle, item_id)
        with open(dest_path, "wb") as f:
            f.write(data[:max_bytes])
        return min(len(data), max_bytes)

    async def get_thumbnail(self, handle, item_id, max_edge_px):
        self._get(handle, item_id)
        return None

    async def media_source(self, handle, item_id):
        self._get(handle, item_id)
        return MediaSource(url=f"https://files.example.com/{handle.connection_id}/{item_id}")

    async def capabilities(self, handle):
        self._check(handle)
        return DriveCapabilities(max_path_chars=self._max)

    async def forget(self, handle):
        self._check(handle)
        for key in [k for k in self._items if k[0] == handle.connection_id]:
            del self._items[key]


def _spec(kind: str) -> ConnectorSpec:
    return ConnectorSpec(kind=kind, integration=Integration.DRIVE, realm=f"realm_{kind}",
                         required_scopes=("files",), multiple=True, id_space=kind, label=kind)


def _conn(connection_id: str, kind: str, tag: str) -> DriveConnection:
    return DriveConnection(connection_id=connection_id, user_id="usr-1", account_id="acc-1",
                           credential_id=f"cred-{connection_id}", kind=kind, drive_identity=f"{kind}:{connection_id}",
                           ref_tag=tag, created_at=NOW)


A1, A2, B1 = _conn("conn-1", "fake_a", "a1"), _conn("conn-2", "fake_a", "b2"), _conn("conn-3", "fake_b", "c3")


@pytest.fixture
def world():
    drives = {"fake_a": _MemDrive("fake_a", 400), "fake_b": _MemDrive("fake_b", 250)}
    registry = ConnectorRegistry(specs=(_spec("fake_a"), _spec("fake_b")), drive_adapters=drives)
    return DriveProviderRouter(registry.drive_adapters()), drives


class TestRoutesByHandle:
    async def test_each_connection_keeps_its_own_items(self, world):
        router, _ = world
        roots = {c.connection_id: await router.get_root(c.handle) for c in (A1, A2, B1)}
        saved = await router.upload(A1.handle, roots["conn-1"].item_id, "report.pdf", b"one", "application/pdf")
        other = await router.upload(B1.handle, roots["conn-3"].item_id, "report.pdf", b"three", "application/pdf")
        assert saved.item_id == "item-1" and other.item_id == "item-1"     # the same item id on two connections
        assert await router.download(A1.handle, "item-1") == b"one"
        assert await router.download(B1.handle, "item-1") == b"three"
        assert (await router.get_item(A1.handle, "item-1")).identity == "fake_a:conn-1:item-1"
        assert (await router.get_item(B1.handle, "item-1")).identity == "fake_b:conn-3:item-1"
        with pytest.raises(DriveItemNotFoundError):
            await router.get_item(A2.handle, "item-1")                     # same kind, other connection

    async def test_capabilities_come_from_the_handles_kind(self, world):
        router, _ = world
        assert await router.capabilities(A1.handle) == DriveCapabilities(max_path_chars=400)
        assert await router.capabilities(B1.handle) == DriveCapabilities(max_path_chars=250)

    async def test_forget_drops_one_connection_only(self, world):
        router, _ = world
        for conn in (A1, A2):
            root = await router.get_root(conn.handle)
            await router.upload(conn.handle, root.item_id, "report.pdf", b"x", "application/pdf")
        await router.forget(A1.handle)
        assert await router.search(A1.handle, "report", 10) == []
        assert [i.name for i in await router.search(A2.handle, "report", 10)] == ["report.pdf"]

    async def test_a_round_trip_through_every_read(self, world, tmp_path):
        router, _ = world
        root = await router.get_root(B1.handle)
        folder = await router.create_folder(B1.handle, root.item_id, "Docs")
        item = await router.upload(B1.handle, folder.item_id, "report.pdf", b"abcdef", "application/pdf")
        assert [i.item_id for i in await router.list_children(B1.handle, folder.item_id)] == [item.item_id]
        assert (await router.child_by_name(B1.handle, folder.item_id, "report.pdf")).item_id == item.item_id
        assert await router.download_to_file(B1.handle, item.item_id, str(tmp_path / "f"), 4) == 4
        assert (tmp_path / "f").read_bytes() == b"abcd"
        assert (await router.media_source(B1.handle, item.item_id)).header_free
        assert (await router.get_change(B1.handle, item.item_id)).identity == "fake_b:conn-3:" + item.item_id
        page = await router.list_children_changes(B1.handle, folder.item_id, 10)
        assert [c.item_id for c in page.changes] == [item.item_id]
        assert (await router.get_changes(B1.handle, None)).drive_identity == "fake_b:conn-3"
        assert await router.area_path(B1.handle) == "/fake_b"
        assert await router.get_thumbnail(B1.handle, item.item_id, 2048) is None
        await router.replace_content(B1.handle, item.item_id, b"new", "application/pdf")
        assert await router.download(B1.handle, item.item_id) == b"new"
        await router.move(B1.handle, item.item_id, new_parent_id=root.item_id)
        assert [i.item_id for i in await router.list_children(B1.handle, root.item_id)] == [folder.item_id,
                                                                                             item.item_id]
        await router.delete(B1.handle, item.item_id)
        with pytest.raises(DriveItemNotFoundError):
            await router.get_item(B1.handle, item.item_id)

    async def test_an_unregistered_kind_is_an_unusable_connection(self, world):
        router, _ = world
        stray = DriveHandle(user_id="usr-1", connection_id="conn-7", credential_id="cred-7", kind="fake_z")
        with pytest.raises(DriveNotConnectedError):
            await router.get_item(stray, "item-1")

    def test_kinds(self, world):
        router, _ = world
        assert router.kinds() == ["fake_a", "fake_b"]

    def test_a_router_is_not_an_adapter(self, world):
        router, _ = world
        with pytest.raises(ValueError):
            DriveProviderRouter({"fake_a": router})


class _Echo(UserDrivePort):
    """Returns what it was called with, tagged with its kind: the router's answer shows which adapter served it."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def __getattribute__(self, name):
        if name in _ROUTED:
            async def echo(*args, **kwargs):
                return (object.__getattribute__(self, "kind"), name, args, kwargs)
            return echo
        return object.__getattribute__(self, name)

    # the abstract members, never reached (served by __getattribute__)
    get_root = get_item = list_children = search = download = upload = replace_content = move = None
    create_folder = delete = child_by_name = area_path = get_changes = get_change = None
    list_children_changes = download_to_file = get_thumbnail = None


_ROUTED = sorted(name for name, member in vars(UserDrivePort).items()
                 if inspect.iscoroutinefunction(member) and name not in SINGLE_DRIVE_MEMBERS)


class TestT8:
    def test_every_handle_member_of_the_port_is_routed(self):
        assert set(_ROUTED) == {
            "get_root", "get_item", "list_children", "search", "download", "upload", "replace_content", "move",
            "create_folder", "delete", "child_by_name", "area_path", "get_changes", "get_change",
            "list_children_changes", "download_to_file", "get_thumbnail", "media_source", "capabilities", "forget"}
        assert not DriveProviderRouter.__abstractmethods__
        assert all(name in vars(DriveProviderRouter) for name in _ROUTED)
        assert not (SINGLE_DRIVE_MEMBERS & set(vars(DriveProviderRouter)))

    @pytest.mark.parametrize("name", _ROUTED)
    async def test_dispatch_passes_the_handle_and_arguments_unchanged(self, name):
        router = DriveProviderRouter({"fake_a": _Echo("fake_a"), "fake_b": _Echo("fake_b")})
        params = list(inspect.signature(getattr(UserDrivePort, name)).parameters)[2:]
        args = tuple(f"arg-{p}" for p in params)
        got = await getattr(router, name)(B1.handle, *args)
        assert got == ("fake_b", name, (B1.handle, *args), {})

    @pytest.mark.parametrize("name", _ROUTED)
    def test_each_body_is_one_dispatch_call(self, name):
        fn = ast.parse(textwrap.dedent(inspect.getsource(getattr(DriveProviderRouter, name)))).body[0]
        body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        assert len(body) == 1 and isinstance(body[0], ast.Return)
        call = body[0].value.value                                          # return await <call>
        assert isinstance(body[0].value, ast.Await) and isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute) and call.func.attr == name
        route = call.func.value
        assert isinstance(route, ast.Call) and route.func.attr == "_route"
        assert [a.id for a in route.args] == ["handle"]
        assert [a.id for a in call.args] == [a.arg for a in fn.args.args[1:]]


class TestRegistryDriveAdapters:
    def test_drive_adapter_by_kind(self):
        drive = _MemDrive("fake_a", 400)
        registry = ConnectorRegistry(specs=(_spec("fake_a"),), drive_adapters={"fake_a": drive})
        assert registry.drive_adapter("fake_a") is drive
        assert dict(registry.drive_adapters()) == {"fake_a": drive}
        with pytest.raises(KeyError):
            registry.drive_adapter("fake_b")

    def test_the_production_registry_has_no_drive_adapter_until_wired(self):
        registry = ConnectorRegistry()
        assert dict(registry.drive_adapters()) == {}
        assert registry.get("microsoft_onedrive").id_space == "microsoft_onedrive"

    def test_production_drive_adapter_needs_a_drive_kind(self):
        with pytest.raises(ValueError):
            ConnectorRegistry(drive_adapters={"gmail": _MemDrive("gmail", 1)})
        with pytest.raises(ValueError):
            ConnectorRegistry(drive_adapters={"dropbox": _MemDrive("dropbox", 1)})

    @pytest.mark.parametrize("specs", [
        (_spec("fake_a"), _spec("fake_a")),                                   # two specs of one kind
        (_spec("fake_a"), ConnectorSpec(kind="fake_b", integration=Integration.DRIVE, realm="r",
                                        required_scopes=(), multiple=True, id_space="fake_a", label="b")),
        (ConnectorSpec(kind="fake_c", integration=Integration.DRIVE, realm="r", required_scopes=(),
                       multiple=True, id_space="", label="c"),),             # a drive without an id space
        (ConnectorSpec(kind="fake_d", integration=Integration.DRIVE, realm="r", required_scopes=(),
                       multiple=True, id_space="fa:ke", label="d"),),        # an id space with ':'
    ])
    def test_an_inconsistent_registry_is_refused(self, specs):
        with pytest.raises(ValueError):
            ConnectorRegistry(specs=specs)

    def test_fake_kinds_are_registered_by_spec_alone(self):
        registry = ConnectorRegistry(specs=(_spec("fake_a"), _spec("fake_b")))
        assert registry.kinds(Integration.DRIVE) == ["fake_a", "fake_b"]
        assert registry.get("fake_b").id_space == "fake_b"
